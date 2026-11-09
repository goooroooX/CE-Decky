#!/usr/bin/env python3
"""What hashing one game executable costs, through the production reader.

The compatibility fallback compares a game by the SHA-256 of its executable
when the executable declares no version and Steam has no build ID for it. Its
bounds - the largest file, the deadline, the queue - are chosen from what this
prints rather than from the one executable that prompted it.

For an explicit executable it reports:

- each full read through `atomic.sha256_regular_file`, with wall and CPU time.
  The first is reported apart from the rest because nothing here establishes
  what the page cache held before it: it is the first read in this run, not a
  cold read, and nothing is dropped from the cache to make it one;
- what a status read pays once the digest is known: one `lstat` and one cache
  lookup, over many calls;
- how long a lookup takes while the worker is reading, with the reader slowed
  per chunk so the read is certainly still in progress, and how long the worker
  takes to leave after `close()`, which is the cooperative stop between chunks;
- with `--pid`, what proving that running processes map this exact file costs,
  uncached and then cached.

Read-only, and it executes nothing. It has no platform requirement; `--pid`
needs a Linux `/proc`.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py_modules"))

from ce_decky import __version__  # noqa: E402
from ce_decky.atomic import sha256_regular_file  # noqa: E402
from ce_decky.executable_digest import ExecutableDigests, regular_file_identity  # noqa: E402


def _spread(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {"min": round(min(values), 4), "median": round(statistics.median(values), 4), "max": round(max(values), 4)}


def _read_once(path: Path) -> dict[str, object]:
    wall, cpu = time.perf_counter(), time.thread_time()
    try:
        digest, info = sha256_regular_file(path, max_bytes=1 << 62)
    except (OSError, ValueError) as exc:
        return {"outcome": "unavailable", "reason": str(exc)}
    return {
        "outcome": "ready", "digest": digest, "size": info.st_size,
        "wall_s": time.perf_counter() - wall, "cpu_s": time.thread_time() - cpu,
    }


def _wait(digests: ExecutableDigests, path: str, identity, bound: float) -> str:
    deadline = time.monotonic() + bound
    while time.monotonic() < deadline:
        answer = digests.lookup(path, identity)
        if answer.state != "pending":
            return answer.state
        time.sleep(0.01)
    return "pending"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("executable", type=Path, help="the exact executable to read")
    parser.add_argument("--samples", type=int, default=5, help="full reads to time (default 5)")
    parser.add_argument("--lookups", type=int, default=2000, help="cached status checks to time (default 2000)")
    parser.add_argument("--chunk-delay", type=float, default=0.002,
                        help="seconds added per chunk for the in-progress check (default 0.002)")
    parser.add_argument("--pid", type=int, action="append", default=[],
                        help="a running process of this executable, repeated for each")
    args = parser.parse_args(argv)
    path = args.executable.resolve()
    identity = regular_file_identity(path)
    report: dict[str, object] = {
        "schema": 1,
        "host": {"platform": platform.platform(), "machine": platform.machine(), "python": platform.python_version()},
        "ce_decky": __version__,
        "executable": path.name,
        "size": None if identity is None else identity[2],
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if identity is None:
        report["outcome"] = "unavailable"
        report["reason"] = "not a readable regular file"
        print(json.dumps(report, indent=2))
        return 1

    reads = [_read_once(path) for _ in range(max(1, args.samples))]
    ready = [read for read in reads if read["outcome"] == "ready"]
    report["first_read"] = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in reads[0].items() if k != "digest"}
    report["repeated_reads"] = {
        "samples": len(reads) - 1,
        "wall_s": _spread([read["wall_s"] for read in ready[1:]]),
        "cpu_s": _spread([read["cpu_s"] for read in ready[1:]]),
        "unavailable": [read["reason"] for read in reads[1:] if read["outcome"] != "ready"],
        "digests_agree": len({read["digest"] for read in ready}) <= 1,
    }
    if ready:
        mib = ready[-1]["size"] / (1024 * 1024)
        report["repeated_reads"]["mib_per_s_median"] = (
            None if len(ready) < 2 else round(mib / statistics.median(read["wall_s"] for read in ready[1:]), 1))

    # What a status read pays once the answer is known.
    digests = ExecutableDigests(None)
    settled = _wait(digests, str(path), identity, 120.0)
    timings = []
    for _ in range(max(1, args.lookups)):
        started = time.perf_counter()
        digests.lookup(str(path), regular_file_identity(path))
        timings.append((time.perf_counter() - started) * 1e6)
    report["cached_check"] = {"settled": settled, "calls": len(timings), "per_call_us": _spread(timings)}
    digests.close()

    # A lookup while the worker is certainly reading, and the stop between chunks.
    def slow_reader(*a, **kw):
        stop = kw["should_stop"]
        kw["should_stop"] = lambda: (time.sleep(args.chunk_delay), stop())[1]
        return sha256_regular_file(*a, **kw)

    busy = ExecutableDigests(None, reader=slow_reader)
    busy.lookup(str(path), identity)
    time.sleep(0.2)
    timings = []
    for _ in range(200):
        started = time.perf_counter()
        answer = busy.lookup(str(path), regular_file_identity(path))
        timings.append((time.perf_counter() - started) * 1e6)
    worker = busy._worker
    started = time.perf_counter()
    busy.close()
    if worker is not None:
        worker.join(30)
    report["while_reading"] = {
        "state": answer.state,
        "lookup_us": _spread(timings),
        "worker_left_after_close_s": None if worker is None or worker.is_alive() else round(time.perf_counter() - started, 4),
    }

    if args.pid:
        proof = ExecutableDigests(None)
        started = time.perf_counter()
        proven, reason = proof.running_identity(str(path), tuple(args.pid))
        uncached = time.perf_counter() - started
        started = time.perf_counter()
        proof.running_identity(str(path), tuple(args.pid))
        report["running_identity"] = {
            "proven": proven is not None, "reason": reason,
            "uncached_ms": round(uncached * 1e3, 2),
            "cached_ms": round((time.perf_counter() - started) * 1e3, 3),
        }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
