#!/usr/bin/env python3
"""What the gate's own test suites spend their time on, test by test.

`qa.py` prints what each stage cost, which says that `backend-full` is most of a
release run and nothing about why. This answers the next question: which tests
those seconds are actually in. It exists because the answer is routinely a
handful of them - one run of this found 91 seconds of a 108 second stage in 30
tests out of 906, and the worst of them was a fixture re-summing its own list
rather than anything the product does.

It runs the same suites the gate runs, through the same interpreter `qa.py`
resolves, and adds only a reporting flag: `--durations` for pytest, and vitest's
own per-file timings for the frontend. Nothing here is a separate copy of the
selection logic, so a figure it prints is a figure the gate would spend.

Read only and bounded: it runs tests, writes nothing outside the caches those
tests already use, and takes a `--timeout` it will not sit past.

    python3 scripts/qa_durations.py                 # the backend suite, slowest 25
    python3 scripts/qa_durations.py --top 40        # more of the tail
    python3 scripts/qa_durations.py --frontend      # the component suite by file
    python3 scripts/qa_durations.py --json          # the same thing for a report

A measurement taken with this is worth recording; one taken by hand with a
stopwatch or a pasted `pytest` line is not, because the next person cannot
reproduce it. `docs/DEVELOPMENT.md` carries this command and what it guarantees.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import qa  # noqa: E402  the gate's own interpreter and node resolution, not a copy of them

# One pytest duration line: `18.42s call     tests/test_x.py::test_y`.
_PYTEST_DURATION = re.compile(r"^\s*([0-9.]+)s\s+(call|setup|teardown)\s+(\S+)\s*$")
# One vitest file line: `✓ tests/workflowUi.test.tsx (123 tests) 4567ms`.
_VITEST_FILE = re.compile(r"^\s*[^\s]*\s*(tests/\S+\.test\.tsx?)\s.*?(\d+)ms\s*$")


def backend_durations(top: int, timeout: float) -> dict[str, object]:
    """The slowest backend tests, from the suite the gate's `backend-full` runs."""
    python = qa._development_python()
    command = [
        str(python), "-m", "pytest", "-q", "--tb=short", "--disable-warnings",
        f"--durations={top}",
    ]
    started = time.monotonic()
    completed = subprocess.run(
        command, cwd=ROOT, timeout=timeout, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    elapsed = time.monotonic() - started
    slowest: list[dict[str, object]] = []
    for line in completed.stdout.splitlines():
        match = _PYTEST_DURATION.match(line)
        if match:
            slowest.append({
                "seconds": float(match.group(1)),
                "phase": match.group(2),
                "test": match.group(3),
            })
    return {
        "suite": "backend",
        "command": " ".join(command),
        "ok": completed.returncode == 0,
        "elapsed_seconds": round(elapsed, 2),
        "slowest": slowest,
        # What the named tests account for, which is the number that says
        # whether there is anything worth doing about them.
        "slowest_seconds": round(sum(float(item["seconds"]) for item in slowest), 2),
    }


def frontend_durations(timeout: float) -> dict[str, object]:
    """Each component test file and what it cost, from vitest's own reporter."""
    node = qa._node()
    if not node:
        return {"suite": "frontend", "ok": False, "reason": "Node.js is unavailable"}
    command = [
        str(node), str(ROOT / "node_modules" / "vitest" / "vitest.mjs"),
        "run", "--reporter=basic",
    ]
    started = time.monotonic()
    completed = subprocess.run(
        command, cwd=ROOT, timeout=timeout, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    elapsed = time.monotonic() - started
    files: list[dict[str, object]] = []
    for line in completed.stdout.splitlines():
        match = _VITEST_FILE.match(line)
        if match:
            files.append({"file": match.group(1), "seconds": round(int(match.group(2)) / 1000, 2)})
    files.sort(key=lambda item: item["seconds"], reverse=True)
    return {
        "suite": "frontend",
        "command": " ".join(command),
        "ok": completed.returncode == 0,
        "elapsed_seconds": round(elapsed, 2),
        "slowest": files,
        "slowest_seconds": round(sum(float(item["seconds"]) for item in files), 2),
    }


def _print(report: dict[str, object]) -> None:
    suite = report.get("suite")
    if not report.get("ok"):
        print(f"{suite}: FAILED {report.get('reason', '')}".rstrip())
    print(f"{suite}: {report.get('elapsed_seconds')}s total, "
          f"{report.get('slowest_seconds')}s in the rows below")
    for item in report.get("slowest", []):  # type: ignore[union-attr]
        name = item.get("test") or item.get("file")
        print(f"  {float(item['seconds']):8.2f}s  {name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--top", type=int, default=25, help="how many of the slowest tests to report")
    parser.add_argument("--frontend", action="store_true", help="report the component suite by file as well")
    parser.add_argument("--backend", action="store_true", help="report only the backend suite (the default)")
    parser.add_argument("--json", action="store_true", help="print the whole report as JSON")
    parser.add_argument("--timeout", type=float, default=900.0, help="seconds this will not run past")
    args = parser.parse_args()
    if args.top < 1 or args.top > 500:
        parser.error("--top must be between 1 and 500")
    if args.timeout < 30 or args.timeout > 3600:
        parser.error("--timeout must be between 30 and 3600 seconds")

    reports: list[dict[str, object]] = []
    if args.backend or not args.frontend:
        reports.append(backend_durations(args.top, args.timeout))
    if args.frontend:
        reports.append(frontend_durations(args.timeout))

    if args.json:
        print(json.dumps({"schema": 1, "reports": reports}, indent=2, sort_keys=True))
    else:
        for report in reports:
            _print(report)
    return 0 if all(report.get("ok") for report in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
