#!/usr/bin/env python3
"""Portable local frontend validation/emission when the pinned Node tree is absent.

This is deliberately not a production Decky bundler. It uses any available
TypeScript 5.x compiler plus repository shims to perform strict project checks
and emit inspectable ES modules under build/frontend-fallback. The official
pinned @decky/rollup build remains authoritative for dist/index.js and runs in
normal CI/release before bundle smoke tests or packaging.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "build" / "frontend-fallback"
IMPORT_RE = re.compile(r"(?:from\s+|import\s*\()(['\"])(\.[^'\"]+)\1")


def _tsc() -> str:
    executable = shutil.which("tsc")
    if not executable:
        raise SystemExit("frontend fallback requires a TypeScript 5.x `tsc` on PATH")
    version = subprocess.run([executable, "--version"], capture_output=True, text=True, check=False)
    match = re.search(r"Version\s+(\d+)\.(\d+)\.(\d+)", version.stdout)
    if version.returncode or match is None or int(match.group(1)) != 5:
        raise SystemExit(f"frontend fallback requires TypeScript 5.x; got {version.stdout.strip()!r}")
    return executable


def _relative_imports_resolve() -> None:
    failures: list[str] = []
    for source in sorted((ROOT / "src").rglob("*.ts*")):
        if source.name.endswith(".d.ts"):
            continue
        text = source.read_text(encoding="utf-8")
        for match in IMPORT_RE.finditer(text):
            raw = match.group(2)
            base = source.parent / raw
            candidates = [
                base.with_suffix(".ts"),
                base.with_suffix(".tsx"),
                base / "index.ts",
                base / "index.tsx",
            ]
            if not any(candidate.is_file() for candidate in candidates):
                failures.append(f"{source.relative_to(ROOT)} -> {raw}")
    if failures:
        raise SystemExit(f"frontend fallback found unresolved relative imports: {failures}")


def _emit(tsc: str) -> None:
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)
    command = [
        tsc,
        "-p", str(ROOT / "tsconfig.sandbox.json"),
        "--pretty", "false",
        "--noEmit", "false",
        "--outDir", str(OUT),
        "--declaration", "false",
        "--sourceMap", "false",
    ]
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    if completed.returncode:
        output = ((completed.stdout or "") + (completed.stderr or "")).strip()
        raise SystemExit(output[-4000:] or f"TypeScript fallback exited {completed.returncode}")


def _verify_emit() -> None:
    expected = []
    for source in sorted((ROOT / "src").rglob("*.ts*")):
        if source.name.endswith(".d.ts"):
            continue
        relative = source.relative_to(ROOT / "src").with_suffix(".js")
        expected.append(relative)
    missing = [str(path) for path in expected if not (OUT / path).is_file()]
    if missing:
        raise SystemExit(f"frontend fallback did not emit expected modules: {missing}")

    manifest = {
        "schema": 1,
        "mode": "development-only-es-modules",
        "authoritative_bundle": "dist/index.js via pinned @decky/rollup",
        "modules": [path.as_posix() for path in expected],
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    _relative_imports_resolve()
    tsc = _tsc()
    _emit(tsc)
    _verify_emit()
    print(f"frontend fallback: PASS ({len(list(OUT.rglob('*.js')))} emitted module(s))")
    print(f"output: {OUT.relative_to(ROOT)}")
    print("note: development validation only; official Rollup remains required for dist/package")


if __name__ == "__main__":
    main()
