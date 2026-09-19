#!/usr/bin/env python3
"""Verify the exact plugin ZIP that will be installed on a target device."""
from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = "CE-Decky"
BACKEND_INIT = f"{PACKAGE_ROOT}/py_modules/ce_decky/__init__.py"
MANAGED_CE_MANIFEST = f"{PACKAGE_ROOT}/py_modules/ce_decky/managed_ce_manifest.json"
REQUIRED_FILES = (
    f"{PACKAGE_ROOT}/dist/index.js",
    f"{PACKAGE_ROOT}/plugin.json",
    f"{PACKAGE_ROOT}/package.json",
    f"{PACKAGE_ROOT}/main.py",
    BACKEND_INIT,
    MANAGED_CE_MANIFEST,
    f"{PACKAGE_ROOT}/README.md",
    f"{PACKAGE_ROOT}/LICENSE",
    f"{PACKAGE_ROOT}/THIRD_PARTY_NOTICES.md",
)
REQUIRED_PREFIXES = (
    f"{PACKAGE_ROOT}/py_modules/",
    f"{PACKAGE_ROOT}/licenses/",
)
MAX_PACKAGE_MEMBERS = 20_000
MAX_PACKAGE_DECLARED_BYTES = 512 * 1024 * 1024
MAX_METADATA_BYTES = 256 * 1024
_VERSION_RE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']\s*$', re.MULTILINE)


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_member(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        bool(name)
        and not name.startswith("/")
        and not path.is_absolute()
        and len(path.parts) >= 2
        and path.parts[0] == PACKAGE_ROOT
        and ".." not in path.parts
        and "\\" not in name
        and "\x00" not in name
    )


def _backend_version(text: str) -> str | None:
    matches = _VERSION_RE.findall(text)
    return matches[0] if len(matches) == 1 and matches[0].strip() else None


def inspect_package(path: Path, *, expect_version: str | None = None) -> dict[str, object]:
    """Whether this ZIP is the exact installable artifact of this repository.

    `expect_version` is for the one package that is deliberately not that: the
    build `scripts/build_downgrade_package.py` makes in order to exercise the
    plugin's own update path, which has to call itself older than the release it
    is going to install. It replaces the repository's version in the comparison
    and nothing else: the package's own two version strings must still agree with
    each other and with the version the caller named, so a package that is not
    what it says it is still fails here.
    """
    raw = path.expanduser()
    if raw.is_symlink():
        raise ValueError("plugin package must not be a symlink")
    path = raw.resolve(strict=True)
    if not path.is_file():
        raise ValueError("plugin package must be a regular ZIP file")
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("plugin package must be a regular ZIP file")

    repository_package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    repository_version = repository_package.get("version") if isinstance(repository_package, dict) else None
    expected_version = expect_version if expect_version is not None else repository_version
    if not isinstance(expected_version, str) or not expected_version:
        raise ValueError("repository package version is invalid")
    repository_backend_version = _backend_version(
        (ROOT / "py_modules" / "ce_decky" / "__init__.py").read_text(encoding="utf-8")
    )

    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = [member.filename for member in infos]
            declared_bytes = sum(max(0, member.file_size) for member in infos)
            bounded_for_crc = (
                len(infos) <= MAX_PACKAGE_MEMBERS
                and declared_bytes <= MAX_PACKAGE_DECLARED_BYTES
            )
            bad = archive.testzip() if bounded_for_crc else None
            unsafe = [name for name in names if not _safe_member(name)]
            duplicates = sorted(name for name, count in Counter(names).items() if count > 1)
            symlinks = [
                member.filename for member in infos
                if stat.S_ISLNK((member.external_attr >> 16) & 0xFFFF)
            ]
            required_missing = [name for name in REQUIRED_FILES if name not in names]
            prefix_missing = [prefix for prefix in REQUIRED_PREFIXES if not any(name.startswith(prefix) for name in names)]

            info_by_name: dict[str, zipfile.ZipInfo] = {}
            for member in infos:
                if member.filename not in info_by_name:
                    info_by_name[member.filename] = member
            metadata_names = (f"{PACKAGE_ROOT}/package.json", BACKEND_INIT)
            oversized_metadata = [
                name
                for name in metadata_names
                if name in info_by_name and info_by_name[name].file_size > MAX_METADATA_BYTES
            ]

            def read_metadata(name: str) -> str | None:
                member = info_by_name.get(name)
                if (
                    not bounded_for_crc
                    or member is None
                    or names.count(name) != 1
                    or member.file_size > MAX_METADATA_BYTES
                    or stat.S_ISLNK((member.external_attr >> 16) & 0xFFFF)
                ):
                    return None
                try:
                    return archive.read(member).decode("utf-8")
                except (UnicodeDecodeError, zipfile.BadZipFile):
                    return None

            packaged_text = read_metadata(f"{PACKAGE_ROOT}/package.json")
            if packaged_text is not None:
                try:
                    packaged = json.loads(packaged_text)
                    packaged_version = packaged.get("version") if isinstance(packaged, dict) else None
                except json.JSONDecodeError:
                    packaged_version = None
            else:
                packaged_version = None

            backend_text = read_metadata(BACKEND_INIT)
            packaged_backend_version = _backend_version(backend_text) if backend_text is not None else None
    except zipfile.BadZipFile as exc:
        raise ValueError(f"plugin package is not a valid ZIP: {exc}") from exc

    errors: list[str] = []
    forbidden_payloads = sorted(
        info.filename for info in infos if info.filename.casefold().endswith((".exe", ".ct"))
    )
    if len(infos) > MAX_PACKAGE_MEMBERS:
        errors.append(f"package contains too many members: {len(infos)} > {MAX_PACKAGE_MEMBERS}")
    if declared_bytes > MAX_PACKAGE_DECLARED_BYTES:
        errors.append(
            f"package declared size is too large: {declared_bytes} > {MAX_PACKAGE_DECLARED_BYTES}"
        )
    if oversized_metadata:
        errors.append(
            f"package metadata members exceed {MAX_METADATA_BYTES} bytes: {oversized_metadata!r}"
        )
    if bounded_for_crc and bad:
        errors.append(f"ZIP CRC check failed at {bad}")
    if unsafe:
        errors.append(f"unsafe/outside-root member names: {unsafe[:8]!r}")
    if duplicates:
        errors.append(f"duplicate member names are not allowed: {duplicates[:8]!r}")
    if symlinks:
        errors.append(f"symlink members are not allowed: {symlinks[:8]!r}")
    if required_missing:
        errors.append(f"required files missing: {required_missing!r}")
    if forbidden_payloads:
        errors.append(f"package vendors forbidden CE/table payloads: {forbidden_payloads[:8]!r}")
    if prefix_missing:
        errors.append(f"required directory payloads missing: {prefix_missing!r}")
    # The repository's own two version strings are checked against each other
    # whatever the caller expects of the package, because a checkout that
    # disagrees with itself is a finding about the checkout.
    if repository_backend_version != repository_version:
        errors.append(
            f"repository backend version mismatch: package={repository_version!r} backend={repository_backend_version!r}"
        )
    if packaged_version != expected_version:
        errors.append(
            f"packaged version mismatch: repository={expected_version!r} package={packaged_version!r}"
        )
    if packaged_backend_version != expected_version:
        errors.append(
            f"packaged backend version mismatch: repository={expected_version!r} backend={packaged_backend_version!r}"
        )

    return {
        "schema": 2,
        "ok": not errors,
        "artifact": str(path),
        "sha256": _sha256(path),
        "bytes": info.st_size,
        "declared_uncompressed_bytes": declared_bytes,
        "version": expected_version,
        "repository_version": repository_version,
        "expected_version_source": "caller" if expect_version is not None else "repository",
        "repository_backend_version": repository_backend_version,
        "packaged_version": packaged_version,
        "packaged_backend_version": packaged_backend_version,
        "member_count": len(names),
        "crc_checked": bounded_for_crc,
        "metadata_limit_bytes": MAX_METADATA_BYTES,
        "package_root": PACKAGE_ROOT,
        "required_files": list(REQUIRED_FILES),
        "required_prefixes": list(REQUIRED_PREFIXES),
        "errors": errors,
    }


def _parser() -> argparse.ArgumentParser:
    repository_package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    version = repository_package["version"]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "package",
        nargs="?",
        default=str(ROOT / "artifacts" / f"CE-Decky-v{version}.zip"),
    )
    parser.add_argument(
        "--expect-version",
        help=(
            "the version this package should carry, instead of the repository's. "
            "For the deliberately older build that exercises the plugin's update path"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = inspect_package(Path(args.package), expect_version=args.expect_version)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"target package probe: {exc}", file=sys.stderr)
        return 2
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
