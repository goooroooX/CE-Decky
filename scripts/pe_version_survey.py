#!/usr/bin/env python3
"""Survey what real Windows executables declare, and what reading one costs.

The version reader is bounded per structure rather than per file, and every one
of those bounds is a claim about what real executables do: how far into its
resource section a file keeps the directories that name its version block, where
the data entries those directories point at actually sit, and how many spans one
read costs. This walks a directory of `.exe` files through the production reader
and reports exactly that, so a bound is chosen from the corpus rather than from
what the format permits.

Read-only, and it executes nothing: the reader parses headers and resources and
never maps or runs the file. It has no platform requirement, so it runs on the
device, on a desktop and in CI alike - what differs is which executables are
there to be read, which is why the report names the host it ran on.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py_modules"))

import ce_decky.pe_version as pe_version  # noqa: E402

# The reader's internals are the subject here rather than an implementation
# detail this reaches around: how far the walk went and how many spans it cost
# are what the bounds are chosen from, and neither is visible from its result.
from ce_decky.pe_version import _Ranges, _find_version_entry, _version_from_pe  # noqa: E402


def survey_one(path: Path) -> dict[str, object]:
    """One executable: what it declares, and what reaching that cost."""
    reach = 0
    walk = _find_version_entry

    def traced(ranges, window, root, node, depth):
        nonlocal reach
        reach = max(reach, node - root + 16)
        return walk(ranges, window, root, node, depth)

    pe_version._find_version_entry = traced
    ranges = _Ranges(path)
    started = time.perf_counter()
    try:
        version = _version_from_pe(ranges)
    except (OSError, ValueError) as exc:
        version, refused = None, str(exc)
    else:
        refused = None
    finally:
        pe_version._find_version_entry = walk
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "version": version,
        "refused": refused,
        "resource_bytes": _resource_bytes(path),
        "directory_reach": reach,
        "spans": ranges._spans,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
    }


def _resource_bytes(path: Path) -> int | None:
    """The size the file declares for its whole resource section."""
    try:
        head, _ = pe_version.read_regular_range(path, offset=0, length=4096)
    except (OSError, ValueError):
        return None
    window = pe_version._Window(head, 0)
    try:
        if not window.has(0, 64) or window.read(0, 2) != b"MZ":
            return None
        pe = window.u32(0x3C)
        if not window.has(pe, 24) or window.read(pe, 4) != b"PE\x00\x00":
            return None
        optional = pe + 24
        magic = window.u16(optional)
        directories_at = optional + (96 if magic == 0x10B else 112)
        if magic not in (0x10B, 0x20B) or not window.has(directories_at, 24):
            return None
        return window.u32(directories_at + 2 * 8 + 4)
    except ValueError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--root",
        action="append",
        required=True,
        type=Path,
        help="directory to walk for executables; repeatable, and never guessed for you",
    )
    parser.add_argument("--pattern", default="**/*.exe", help="glob applied under each root (default: %(default)s)")
    parser.add_argument("--limit", type=int, default=0, help="stop after this many executables (0: no limit)")
    parser.add_argument("--json", action="store_true", help="print the whole report as JSON")
    args = parser.parse_args()

    found: list[Path] = []
    for root in args.root:
        if not root.is_dir():
            print(f"pe-version-survey: not a directory: {root}", file=sys.stderr)
            return 2
        found += [path for path in sorted(root.glob(args.pattern)) if path.is_file()]
    seen: list[Path] = []
    for path in found:
        if path not in seen:
            seen.append(path)
    if args.limit > 0:
        seen = seen[: args.limit]
    if not seen:
        print("pe-version-survey: no executables under the roots given", file=sys.stderr)
        return 1

    rows = [survey_one(path) for path in seen]
    declared = [row for row in rows if row["version"]]
    report = {
        "host": f"{platform.system()} {platform.machine()} python {platform.python_version()}",
        "executables": len(rows),
        "declared": len(declared),
        "max_directory_reach": max(int(row["directory_reach"]) for row in rows),
        "max_resource_bytes": max((int(row["resource_bytes"]) for row in rows if row["resource_bytes"]), default=0),
        "max_spans": max(int(row["spans"]) for row in rows),
        "max_bytes": max(int(row["bytes"]) for row in rows),
        "total_ms": round(sum(float(row["elapsed_ms"]) for row in rows), 3),
        "rows": rows,
    }
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    for row in sorted(rows, key=lambda row: int(row["directory_reach"]), reverse=True):
        print(
            f"{str(row['version'] or row['refused'] or 'no version')[:34]:<36}"
            f" reach {row['directory_reach']:>7}"
            f" rsrc {str(row['resource_bytes']):>9}"
            f" spans {row['spans']:>2}"
            f" {row['elapsed_ms']:>8.3f} ms  {Path(str(row['path'])).name}"
        )
    print(
        f"{report['executables']} executables, {report['declared']} declare a version;"
        f" largest {report['max_bytes']} bytes, resource section {report['max_resource_bytes']},"
        f" directory reach {report['max_directory_reach']}, {report['max_spans']} spans at most,"
        f" {report['total_ms']} ms in total, on {report['host']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
