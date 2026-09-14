#!/usr/bin/env python3
"""Resolve one exact existing Steam compatibility prefix for an AppID.

The probe is read-only. It discovers current Steam library roots from standard roots
plus libraryfolders.vdf, then considers only exact steamapps/compatdata/<AppID>/pfx
paths. Incomplete/unsafe library metadata, unavailable declared libraries, or an
unsafe exact prefix blocks resolution; the probe never drops an ambiguous observation
merely to produce one candidate.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import stat
import sys

if __package__:
    from . import host_platform
else:
    import host_platform

_PATH_LINE = re.compile(r'^\s*"path"\s+"((?:\\.|[^"\\])*)"\s*$')
MAX_LIBRARY_VDF_BYTES = 4 * 1024 * 1024


def _decode_kv_string(value: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        if index + 1 >= len(value):
            raise ValueError("truncated escape in libraryfolders.vdf path")
        next_char = value[index + 1]
        if next_char not in {'\\', '"'}:
            raise ValueError(f"unsupported escape in libraryfolders.vdf path: \\{next_char}")
        out.append(next_char)
        index += 2
    return "".join(out)


def _standard_steam_roots(home: Path) -> list[Path]:
    return [home / ".local/share/Steam", home / ".steam/steam"]


def _read_library_paths(vdf: Path) -> list[Path]:
    if vdf.is_symlink():
        raise ValueError(f"libraryfolders.vdf must not be a symlink: {vdf}")
    info = vdf.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_LIBRARY_VDF_BYTES:
        raise ValueError(f"libraryfolders.vdf is not a bounded regular file: {vdf}")
    try:
        lines = vdf.read_text(encoding="utf-8", errors="strict").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot read libraryfolders.vdf safely: {vdf}: {exc}") from exc
    paths: list[Path] = []
    for lineno, line in enumerate(lines, 1):
        if '"path"' not in line:
            continue
        match = _PATH_LINE.match(line)
        if not match:
            raise ValueError(f"malformed library path line in {vdf} at line {lineno}")
        decoded = _decode_kv_string(match.group(1))
        if not decoded:
            raise ValueError(f"empty library path in {vdf} at line {lineno}")
        paths.append(Path(decoded))
    return paths


def discover_library_roots(home: Path) -> tuple[list[Path], list[str], list[str]]:
    roots: list[Path] = []
    sources: list[str] = []
    unavailable: list[str] = []
    seen: set[str] = set()
    seen_unavailable: set[str] = set()

    def add(raw: Path, source: str, *, declared: bool) -> None:
        expanded = raw.expanduser()
        try:
            resolved = expanded.resolve(strict=True)
        except OSError:
            if declared:
                key = str(expanded)
                if key not in seen_unavailable:
                    seen_unavailable.add(key)
                    unavailable.append(key)
            return
        if not resolved.is_dir():
            if declared:
                key = str(resolved)
                if key not in seen_unavailable:
                    seen_unavailable.add(key)
                    unavailable.append(key)
            return
        key = str(resolved)
        if key in seen:
            return
        seen.add(key)
        roots.append(resolved)
        sources.append(source)

    steam_roots = _standard_steam_roots(home)
    for steam_root in steam_roots:
        add(steam_root, "standard Steam root", declared=False)

    seen_vdf: set[str] = set()
    for steam_root in steam_roots:
        vdf = steam_root / "steamapps" / "libraryfolders.vdf"
        if not vdf.exists() and not vdf.is_symlink():
            continue
        try:
            vdf_key = str(vdf.resolve(strict=True))
        except OSError as exc:
            raise ValueError(f"cannot resolve libraryfolders.vdf: {vdf}: {exc}") from exc
        if vdf_key in seen_vdf:
            continue
        seen_vdf.add(vdf_key)
        for library in _read_library_paths(vdf):
            add(library, vdf_key, declared=True)
    return roots, sources, unavailable


def _unsafe_exact_component(path: Path) -> bool:
    """Return True when an existing exact-prefix component is not a real directory."""
    if path.is_symlink():
        return True
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return not stat.S_ISDIR(info.st_mode)


def resolve_prefix(app_id: int, home: Path) -> dict[str, object]:
    if isinstance(app_id, bool) or not isinstance(app_id, int) or app_id < 1 or app_id > 0xFFFFFFFF:
        raise ValueError("AppID must be an integer between 1 and 4294967295")
    raw_home = home.expanduser()
    if raw_home.is_symlink():
        raise ValueError("home path must not be a symlink")
    home = raw_home.resolve(strict=True)
    if not home.is_dir():
        raise ValueError("home path must be a directory")
    libraries, sources, unavailable_libraries = discover_library_roots(home)
    candidates: list[str] = []
    unsafe_exact_paths: list[str] = []
    seen: set[str] = set()

    for library in libraries:
        steamapps = library / "steamapps"
        compatdata = steamapps / "compatdata"
        app_root = compatdata / str(app_id)
        raw = app_root / "pfx"
        exact_components = (steamapps, compatdata, app_root, raw)
        unsafe_component = next(
            (component for component in exact_components if _unsafe_exact_component(component)),
            None,
        )
        if unsafe_component is not None:
            unsafe_exact_paths.append(str(unsafe_component))
            continue
        if not raw.is_dir():
            continue
        try:
            resolved = raw.resolve(strict=True)
        except OSError:
            unsafe_exact_paths.append(str(raw))
            continue
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(key)

    selected = candidates[0] if len(candidates) == 1 and not unsafe_exact_paths and not unavailable_libraries else None
    if unsafe_exact_paths or unavailable_libraries:
        state = "unsafe"
    elif selected:
        state = "resolved"
    elif not candidates:
        state = "missing"
    else:
        state = "ambiguous"

    cmd_path = None
    if selected:
        cmd = Path(selected) / "drive_c" / "windows" / "system32" / "cmd.exe"
        if cmd.is_file() and not cmd.is_symlink():
            cmd_path = str(cmd.resolve())
    return {
        "schema": 2,
        "app_id": app_id,
        "state": state,
        "selected_prefix": selected,
        "candidates": candidates,
        "unsafe_exact_paths": unsafe_exact_paths,
        "unavailable_library_paths": unavailable_libraries,
        "library_roots": [str(path) for path in libraries],
        "library_sources": sources,
        "prefix_cmd_exe": cmd_path,
        "ok": state == "resolved",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--appid", type=int, required=True)
    parser.add_argument(
        "--home",
        type=Path,
        required=True,
        help="exact DECKY_USER_HOME observed from the live plugin/QAM diagnostics",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        host_platform.require("The compatibility prefix probe", needs_procfs=False)
    except host_platform.UnsupportedHost as exc:
        return host_platform.refuse(exc)
    try:
        report = resolve_prefix(args.appid, args.home)
    except (OSError, ValueError) as exc:
        print(f"target prefix probe: {exc}", file=sys.stderr)
        return 2
    json.dump(report, sys.stdout, indent=2, sort_keys=True, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
