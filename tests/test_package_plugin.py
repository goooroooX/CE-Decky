"""The release archive is a function of its contents, not of a working tree.

A release is attested, and an attestation is a reproducibility claim: anyone
holding the tagged commit should be able to build the same bytes. That failed
once here in a way nothing reported. Git records one permission bit, so a file
left at 0600 in one checkout and 0644 in another is the same file to Git and was
two different archive members to the packager, and the two trees produced
different SHA-256 for identical content.
"""
from __future__ import annotations

from pathlib import Path
import hashlib
import zipfile

from scripts import package_plugin


def _archive(tmp_path: Path, name: str, mode: int) -> bytes:
    source = tmp_path / f"{name}-source"
    source.mkdir()
    member = source / "module.py"
    member.write_text("value = 1\n", encoding="utf-8")
    member.chmod(mode)
    target = tmp_path / f"{name}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as packed:
        package_plugin._write_reproducible_member(packed, member, Path("CE-Decky/module.py"))
    return target.read_bytes()


def test_two_checkouts_of_one_file_produce_one_archive(tmp_path: Path):
    restrictive = _archive(tmp_path, "restrictive", 0o600)
    ordinary = _archive(tmp_path, "ordinary", 0o644)

    assert hashlib.sha256(restrictive).hexdigest() == hashlib.sha256(ordinary).hexdigest()


def test_the_executable_bit_git_does_record_is_kept(tmp_path: Path):
    """Git tracks this one, so it is content and it survives into the archive."""
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "module.py").write_text("value = 1\n", encoding="utf-8")
    (plain / "module.py").chmod(0o644)
    runnable = tmp_path / "runnable"
    runnable.mkdir()
    (runnable / "module.py").write_text("value = 1\n", encoding="utf-8")
    (runnable / "module.py").chmod(0o755)

    modes = []
    for directory in (plain, runnable):
        target = directory / "out.zip"
        with zipfile.ZipFile(target, "w") as packed:
            package_plugin._write_reproducible_member(
                packed, directory / "module.py", Path("CE-Decky/module.py")
            )
        modes.append(zipfile.ZipFile(target).infolist()[0].external_attr >> 16 & 0o777)

    assert modes == [0o644, 0o755]


def test_a_symlink_is_refused_rather_than_followed(tmp_path: Path):
    target = tmp_path / "real.py"
    target.write_text("value = 1\n", encoding="utf-8")
    link = tmp_path / "link.py"
    link.symlink_to(target)

    with zipfile.ZipFile(tmp_path / "out.zip", "w") as packed:
        try:
            package_plugin._write_reproducible_member(packed, link, Path("CE-Decky/link.py"))
        except SystemExit as refused:
            assert "non-regular member" in str(refused)
        else:
            raise AssertionError("a symlink was packaged")
