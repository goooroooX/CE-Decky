#!/usr/bin/env python3
"""Build and check the artifact the official Decky Store would ship.

This project's own `scripts/package_plugin.py` and the Decky CLI are different
build paths, and CI validating the first says nothing about the second. The CLI
copies `py_modules/` as the repository holds it, installs no Python
dependencies, packages a fixed set of top-level files plus `dist/`, `bin/`,
`defaults/` and `py_modules/`, and strips the `defaults/` prefix. Everything
this checks is a way that path can produce an installable-looking ZIP whose
backend is dead or whose listing is blank.

`--build` runs the pinned CLI through a container engine, which is how the
plugin database builds it:

    decky plugin build -b -o <output> -s directory .

`-s directory` names the archive and its root folder after the checkout
directory, exactly as the database does with its submodule directory, so the
root here is whatever this repository is checked out as.
"""
from __future__ import annotations

from pathlib import Path
import argparse
import json
import shutil
import subprocess
import sys
import urllib.request
import zipfile

try:
    from scripts.package_plugin import verify_installed_backend
except ModuleNotFoundError:  # Direct `python scripts/check_store_artifact.py` execution.
    from package_plugin import verify_installed_backend

ROOT = Path(__file__).resolve().parents[1]
# The version `decky-plugin-database` pins in its build workflow. Moving it is a
# deliberate change, because it is the build every submission is judged by.
DECKY_CLI_VERSION = "0.0.7"
DECKY_CLI_URL = (
    f"https://github.com/SteamDeckHomebrew/cli/releases/download/{DECKY_CLI_VERSION}/decky-linux-x86_64"
)
CLI_CACHE = ROOT / "build" / "decky-cli" / f"decky-{DECKY_CLI_VERSION}"
OUTPUT = ROOT / "build" / "decky-store"

REQUIRED_MEMBERS = (
    "dist/index.js",
    "main.py",
    "package.json",
    "plugin.json",
    "README.md",
    "LICENSE",
    # Present only because they live under `defaults/`; this is the assertion
    # that the prefix strip actually happened.
    "THIRD_PARTY_NOTICES.md",
    "licenses/LGPL-2.1.txt",
    "licenses/CPython-3.11.7.txt",
    "py_modules/ce_decky/plugin.py",
    "py_modules/ce_decky/managed_ce_manifest.json",
    "py_modules/ce_decky/ce_decky_bridge.lua",
    "py_modules/stdlib_fallback/xml/etree/ElementTree.py",
    "py_modules/stdlib_fallback/html/parser.py",
    # The whole reason the vendor tree is committed.
    "py_modules/vendor/httpx/__init__.py",
    "py_modules/vendor/bs4/__init__.py",
    "py_modules/vendor/defusedxml/__init__.py",
)
FORBIDDEN_SUFFIXES = (".ct", ".cetrainer", ".exe", ".dll", ".msi", ".7z", ".rar")
# Directories the builder is expected to leave behind. It excludes `src/` itself
# and packages an allowlist, so any of these appearing means the contract moved.
FORBIDDEN_PREFIXES = ("src/", "tests/", "docs/", "scripts/", "tools/", ".git/", "artifacts/", "build/")


def container_engine() -> str | None:
    for engine in ("podman", "docker"):
        if shutil.which(engine):
            return engine
    return None


def _decky_cli() -> Path:
    if CLI_CACHE.is_file():
        return CLI_CACHE
    CLI_CACHE.parent.mkdir(parents=True, exist_ok=True)
    temporary = CLI_CACHE.with_suffix(".partial")
    with urllib.request.urlopen(DECKY_CLI_URL, timeout=120) as response:
        temporary.write_bytes(response.read())
    temporary.chmod(0o755)
    temporary.replace(CLI_CACHE)
    print(f"fetched Decky CLI {DECKY_CLI_VERSION}", file=sys.stderr)
    return CLI_CACHE


def build(engine: str) -> Path:
    shutil.rmtree(OUTPUT, ignore_errors=True)
    OUTPUT.mkdir(parents=True)
    subprocess.run(
        [str(_decky_cli()), "plugin", "build", "-b", "-e", engine, "-o", str(OUTPUT), "-s", "directory", str(ROOT)],
        check=True,
        cwd=ROOT,
    )
    archives = sorted(OUTPUT.glob("*.zip"))
    if len(archives) != 1:
        raise SystemExit(f"the Decky CLI produced {len(archives)} archives, expected one: {archives}")
    return archives[0]


def _package_root(names: list[str]) -> str:
    roots = {name.split("/", 1)[0] for name in names if "/" in name}
    if len(roots) != 1:
        raise SystemExit(f"the archive does not have exactly one root directory: {sorted(roots)}")
    return roots.pop()


def check(archive: Path) -> list[str]:
    found: list[str] = []
    with zipfile.ZipFile(archive) as packed:
        corrupt = packed.testzip()
        if corrupt:
            return [f"corrupt member: {corrupt}"]
        names = packed.namelist()
        root = _package_root(names)
        members = {name[len(root) + 1:] for name in names if name.startswith(f"{root}/")}
        files = {name for name in members if name and not name.endswith("/")}

        missing = [name for name in REQUIRED_MEMBERS if name not in files]
        if missing:
            found.append(f"missing from the Store artifact: {missing}")
        payloads = sorted(name for name in files if name.casefold().endswith(FORBIDDEN_SUFFIXES))
        if payloads:
            found.append(f"forbidden payload in the Store artifact: {payloads}")
        leaked = sorted(name for name in files if name.startswith(FORBIDDEN_PREFIXES))
        if leaked:
            found.append(f"development material in the Store artifact: {leaked}")

        manifest = json.loads(packed.read(f"{root}/plugin.json"))
        repository_manifest = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
        if manifest != repository_manifest:
            found.append("the packaged plugin.json is not the repository's")
        publish = manifest.get("publish") or {}
        for field in ("description", "image", "tags"):
            if not publish.get(field):
                found.append(f"plugin.json publish.{field} is empty, so the Store listing would be blank")

        packaged_version = json.loads(packed.read(f"{root}/package.json"))["version"]
        version = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"]
        # The database's workflow appends a commit hash to the version on
        # pull-request builds, so the packaged version may extend ours.
        if packaged_version != version and not packaged_version.startswith(f"{version}-"):
            found.append(f"packaged version {packaged_version} does not come from {version}")

    if not found:
        try:
            verify_installed_backend(archive, root)
        except subprocess.CalledProcessError as failure:
            found.append(f"the packaged backend does not import: {failure.stderr or failure.stdout}")
    return found


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("archive", nargs="?", type=Path, help="a ZIP the Decky CLI already built")
    parser.add_argument("--build", action="store_true", help="build it with the pinned Decky CLI first")
    arguments = parser.parse_args()
    if bool(arguments.archive) == bool(arguments.build):
        raise SystemExit("name an archive or pass --build, not both and not neither")
    if arguments.build:
        engine = container_engine()
        if not engine:
            raise SystemExit("no container engine: the Decky CLI needs podman or docker to build the frontend")
        archive = build(engine)
    else:
        archive = arguments.archive
    found = check(archive)
    for failure in found:
        print(failure, file=sys.stderr)
    if found:
        raise SystemExit(1)
    print(f"store artifact: PASS ({archive.relative_to(ROOT) if archive.is_relative_to(ROOT) else archive})")


if __name__ == "__main__":
    main()
