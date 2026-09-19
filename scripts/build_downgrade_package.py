#!/usr/bin/env python3
"""Build this tree as a package that calls itself an older version.

There is one thing about the self-update that no unit test reaches: a real
device, holding a real earlier build, finding the real published release and
installing it. Producing that earlier build by checking out an older commit
would test the old code rather than the new, and an override that makes this
build lie about its version would put a branch in production that exists only
for a test.

So this packages the current tree, unchanged, with the three version strings set
to a version below the published release. Everything under test is the code that
ships: the check, the digest verification, Decky's install, the interface
restart and the record afterwards. The only difference from a release build is
the number it compares against GitHub.

The artifact is named so it cannot be mistaken for a release, and the version
files are restored from what was read before the build, whatever happens. The
tree must be clean first: a helper that rewrites four tracked files and then
restores them has no way to tell its own edit from yours.

    python scripts/build_downgrade_package.py --version 0.9.27

Then install it the ordinary way, with the SHA-256 this prints:

    python scripts/target_plugin_install.py install --package <artifact> \\
        --sha256 <printed digest> --replace
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import argparse
import json
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"
# What the packager would have called it, renamed so that an artifact claiming
# to be a release and an artifact built to be out of date are never the same
# filename in the same directory.
ARTIFACT_PREFIX = "CE-Decky-downgrade-v"
VERSION_RE = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,6}$")


def _current_version() -> str:
    return json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"]


def _previous_version(version: str) -> str:
    """One below the current patch component, which is how this project counts."""
    major, minor, patch = (int(part) for part in version.split("."))
    if patch == 0:
        raise SystemExit(f"cannot derive a version below {version}; pass --version")
    return f"{major}.{minor}.{patch - 1}"


def _rewrite(version: str, target: str) -> dict[Path, str]:
    """Stamp `target` over `version` in the files that carry it. Returns the originals."""
    edits = {
        ROOT / "package.json": (f'"version": "{version}"', f'"version": "{target}"'),
        ROOT / "py_modules" / "ce_decky" / "__init__.py": (f'__version__ = "{version}"', f'__version__ = "{target}"'),
    }
    originals: dict[Path, str] = {}
    for path, (old, new) in edits.items():
        text = path.read_text(encoding="utf-8")
        if text.count(old) != 1:
            raise SystemExit(f"{path.relative_to(ROOT)} does not carry exactly one {version}")
        originals[path] = text
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return originals


def _tree_is_clean() -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, check=False,
    )
    return result.returncode == 0 and not result.stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--version",
        help="the version to stamp; defaults to one below this tree's own patch component",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="build from a tree with uncommitted changes, restoring the version files afterwards anyway",
    )
    args = parser.parse_args()

    current = _current_version()
    target = args.version or _previous_version(current)
    if not VERSION_RE.fullmatch(target):
        raise SystemExit(f"--version must be a plain released version, not {target!r}")
    if tuple(int(part) for part in target.split(".")) >= tuple(int(part) for part in current.split(".")):
        raise SystemExit(f"{target} is not below this tree's version {current}")
    if not args.allow_dirty and not _tree_is_clean():
        raise SystemExit("the working tree has uncommitted changes; commit them or pass --allow-dirty")

    originals = _rewrite(current, target)
    try:
        subprocess.run([sys.executable, str(ROOT / "scripts" / "package_plugin.py")], cwd=ROOT, check=True)
        built = ARTIFACTS / f"CE-Decky-v{target}.zip"
        if not built.is_file():
            raise SystemExit(f"the packager did not produce {built.name}")
        artifact = ARTIFACTS / f"{ARTIFACT_PREFIX}{target}.zip"
        shutil.move(str(built), str(artifact))
    finally:
        for path, text in originals.items():
            path.write_text(text, encoding="utf-8")

    digest = sha256()
    with artifact.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    print(f"downgrade package: {artifact}")
    print(f"sha256: {digest.hexdigest()}")
    print(f"it reports {target}; this tree is {current}")


if __name__ == "__main__":
    main()
