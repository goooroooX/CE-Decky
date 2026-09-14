#!/usr/bin/env python3
"""Exercise CE Decky's production .7z adapter against the target host tool.

The probe creates one disposable benign Unicode .7z fixture. ZIP safety and the
intentional rejection of passworded .7z extraction are ordinary off-target
regressions; the Steam Machine only needs to prove the actual host executable's
metadata-list and stdout-extraction behavior used by production.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

if __package__:
    from . import host_platform
else:
    import host_platform

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py_modules"))

from ce_decky.archive_import import extract_ct_member, inspect_archive  # noqa: E402

CT_BYTES = b'<?xml version="1.0" encoding="utf-8"?><CheatTable CheatEngineTableVersion="45"><CheatEntries/></CheatTable>\n'


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def find_sevenzip() -> str | None:
    for name in ("7z", "7za", "7zr"):
        path = shutil.which(name)
        if path:
            return str(Path(path).resolve())
    return None


def _run(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(command, 127, f"{type(exc).__name__}: {exc}", "")


def _sevenzip_checks(root: Path, sevenzip: str) -> tuple[list[Check], str]:
    checks: list[Check] = []
    info = _run([sevenzip, "i"])
    info_text = "\n".join(info.stdout.splitlines()[:6]).strip()
    checks.append(Check("sevenzip_info", info.returncode == 0, info_text or f"exit={info.returncode}"))

    source_name = "Zażółć_日本.CT"
    source = root / source_name
    source.write_bytes(CT_BYTES)
    archive = root / "unicode.7z"
    # Fixture creation is not a production contract. Names are fixed benign local
    # values, so avoid requiring any add-command switch terminator behavior here.
    created = _run([sevenzip, "a", "-t7z", "-y", archive.name, source.name], cwd=root)
    if created.returncode != 0:
        checks.append(Check("sevenzip_unicode_roundtrip", False, created.stdout[-1000:]))
        return checks, info_text

    try:
        inspection = inspect_archive(archive, sevenzip=sevenzip)
        destination = root / "sevenzip-unicode-extracted.ct"
        extract_ct_member(archive, source_name, destination, sevenzip=sevenzip)
        checks.append(Check(
            "sevenzip_unicode_roundtrip",
            any(member.path == source_name for member in inspection.members)
            and destination.read_bytes() == CT_BYTES,
            f"members={[member.path for member in inspection.members]!r}",
        ))
    except Exception as exc:
        checks.append(Check("sevenzip_unicode_roundtrip", False, f"{type(exc).__name__}: {exc}"))
    return checks, info_text


def run_probe(sevenzip: str) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="ce-decky-target-archive-") as temporary:
        checks, info = _sevenzip_checks(Path(temporary), sevenzip)
    return {
        "schema": 2,
        "sevenzip": sevenzip,
        "sevenzip_info": info,
        "fixture_policy": "one generated benign Unicode .7z; ZIP/password rejection remains cloud-prevalidated",
        "checks": [asdict(check) for check in checks],
        "ok": all(check.ok for check in checks),
    }


def main() -> int:
    try:
        host_platform.require("The archive adapter probe", needs_procfs=False)
    except host_platform.UnsupportedHost as exc:
        return host_platform.refuse(exc)
    sevenzip = find_sevenzip()
    if not sevenzip:
        payload = {
            "schema": 2,
            "ok": False,
            "blocked": True,
            "reason": "7z/7za/7zr not found on target host",
        }
        json.dump(payload, sys.stdout, indent=2, sort_keys=True, ensure_ascii=False)
        sys.stdout.write("\n")
        return 2
    report = run_probe(sevenzip)
    json.dump(report, sys.stdout, indent=2, sort_keys=True, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
