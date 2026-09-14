#!/usr/bin/env python3
"""Compute and optionally stamp the frontend source/toolchain digest."""

from __future__ import annotations

from argparse import ArgumentParser
import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STAMP = ROOT / "dist" / ".ce-decky-source.sha256"
CONTROL_FILES = (
    ROOT / "package.json",
    ROOT / "plugin.json",
    ROOT / "pnpm-lock.yaml",
    ROOT / "rollup.config.js",
    ROOT / "tsconfig.json",
)


def frontend_digest() -> str:
    digest = hashlib.sha256()
    source_files = sorted(path for path in (ROOT / "src").rglob("*") if path.is_file())
    for path in (*CONTROL_FILES, *source_files):
        relative = path.relative_to(ROOT).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        data = path.read_bytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def stamp() -> str:
    value = frontend_digest()
    STAMP.parent.mkdir(parents=True, exist_ok=True)
    STAMP.write_text(value + "\n", encoding="ascii", newline="\n")
    return value


def is_current() -> bool:
    if not STAMP.is_file():
        return False
    return STAMP.read_text(encoding="ascii").strip() == frontend_digest()


def main() -> None:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--stamp", action="store_true", help="write the digest next to the official Rollup bundle")
    parser.add_argument("--check", action="store_true", help="fail unless the bundle stamp matches current frontend sources")
    args = parser.parse_args()
    if args.stamp and args.check:
        parser.error("choose --stamp or --check")
    if args.stamp:
        print(stamp())
        return
    if args.check:
        if not is_current():
            raise SystemExit("dist/index.js is not stamped for the current frontend sources; run the official `pnpm run build`")
        print("frontend bundle digest: PASS")
        return
    print(frontend_digest())


if __name__ == "__main__":
    main()
