from __future__ import annotations

from pathlib import Path
import json
import shutil
import struct
import zipfile

import pytest

from ce_decky.archive_import import ArchiveImportError
from ce_decky.ce_archive_import import (
    IMPORT_MANIFEST_NAME,
    extract_ce_archive,
    inspect_ce_archive,
    promote_imported_installation,
    snapshot_ce_archive,
    validate_imported_installation,
)


def _pe_bytes(size: int = 300 * 1024) -> bytes:
    data = bytearray(b"\x00" * size)
    data[0:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\x00\x00"
    return bytes(data)


def _installation(prefix: str = "") -> dict[str, bytes]:
    members = {
        f"{prefix}cheatengine-x86_64.exe": _pe_bytes(),
        f"{prefix}defines.lua": b"-- defines\n",
        f"{prefix}autorun/ceshare.lua": b"-- autorun\n",
    }
    for index in range(20):
        members[f"{prefix}bin{index}.dll"] = b"payload"
    return members


def _write_zip(path: Path, members: dict[str, bytes], directories: tuple[str, ...] = ()) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name in directories:
            archive.writestr(zipfile.ZipInfo(name), b"")
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def test_locates_the_installation_root_under_arbitrary_nesting(tmp_path: Path):
    source = _write_zip(
        tmp_path / "ce.zip",
        _installation("Cheat Engine 7.7/inner/"),
        directories=("Cheat Engine 7.7/", "Cheat Engine 7.7/inner/", "Cheat Engine 7.7/inner/autorun/"),
    )
    layout = inspect_ce_archive(source)
    assert layout.root == "cheat engine 7.7/inner"
    assert layout.executable == "cheatengine-x86_64.exe"
    assert layout.file_count == 23

    # The root is matched case-insensitively, but the tree is written under the
    # names the archive carries: Cheat Engine ships mixed-case assets.
    stage = tmp_path / "stage"
    extract_ce_archive(source, layout, stage)
    assert sorted(item.name for item in stage.iterdir()) == sorted(
        ["cheatengine-x86_64.exe", "defines.lua", "autorun"] + [f"bin{index}.dll" for index in range(20)]
    )


def test_accepts_an_installation_packed_at_the_archive_root(tmp_path: Path):
    source = _write_zip(tmp_path / "ce.zip", _installation(), directories=("autorun/",))
    layout = inspect_ce_archive(source)
    assert layout.root == ""
    assert layout.file_count == 23


def test_archive_snapshot_keeps_one_exact_source_after_the_selected_file_changes(tmp_path: Path):
    source = _write_zip(tmp_path / "ce.zip", _installation(), directories=("autorun/",))
    snapshot = snapshot_ce_archive(source, tmp_path / "staging")
    source.write_bytes(b"replacement")

    assert inspect_ce_archive(snapshot).file_count == 23


def test_archive_snapshot_preserves_the_user_facing_zip_contract(tmp_path: Path):
    source = _write_zip(tmp_path / "ce.exe", _installation(), directories=("autorun/",))
    with pytest.raises(ArchiveImportError, match="must be a .zip"):
        snapshot_ce_archive(source, tmp_path / "staging")


def test_rejects_an_archive_with_no_cheat_engine_executable(tmp_path: Path):
    members = {name: data for name, data in _installation().items() if "cheatengine" not in name}
    source = _write_zip(tmp_path / "ce.zip", members, directories=("autorun/",))
    with pytest.raises(ArchiveImportError, match="no Cheat Engine executable"):
        inspect_ce_archive(source)


def test_rejects_an_installation_missing_the_files_beside_the_executable(tmp_path: Path):
    members = {name: data for name, data in _installation().items() if not name.endswith("defines.lua")}
    source = _write_zip(tmp_path / "ce.zip", members, directories=("autorun/",))
    with pytest.raises(ArchiveImportError, match="defines.lua is missing"):
        inspect_ce_archive(source)


def test_rejects_a_handful_of_files_packed_beside_the_executable(tmp_path: Path):
    members = {
        "cheatengine-x86_64.exe": _pe_bytes(),
        "defines.lua": b"-- defines\n",
        "autorun/ceshare.lua": b"-- autorun\n",
    }
    source = _write_zip(tmp_path / "ce.zip", members, directories=("autorun/",))
    with pytest.raises(ArchiveImportError, match="Pack the whole installation directory"):
        inspect_ce_archive(source)


def test_rejects_a_traversing_member(tmp_path: Path):
    members = _installation()
    members["../escape.dll"] = b"payload"
    source = _write_zip(tmp_path / "ce.zip", members, directories=("autorun/",))
    with pytest.raises(ArchiveImportError, match="unsafe archive member path"):
        inspect_ce_archive(source)


def test_rejects_a_file_that_is_not_a_zip(tmp_path: Path):
    source = tmp_path / "ce.7z"
    source.write_bytes(b"7z\xbc\xaf\x27\x1c")
    with pytest.raises(ArchiveImportError, match="must be a .zip archive"):
        inspect_ce_archive(source)


def test_writes_the_names_the_archive_carries_not_the_folded_ones(tmp_path: Path):
    members = _installation("Packed/")
    members["Packed/CSCompiler.dll"] = b"payload"
    members["Packed/Extensions/CEAA.dll"] = b"payload"
    source = _write_zip(tmp_path / "ce.zip", members, directories=("Packed/", "Packed/autorun/", "Packed/Extensions/"))
    layout = inspect_ce_archive(source)
    stage = tmp_path / "stage"
    extract_ce_archive(source, layout, stage)
    assert (stage / "CSCompiler.dll").is_file()
    assert (stage / "Extensions" / "CEAA.dll").is_file()


def test_extracts_only_the_installation_and_promotes_it(tmp_path: Path):
    members = _installation("packed/Cheat Engine/")
    members["packed/readme.txt"] = b"not part of the installation"
    source = _write_zip(
        tmp_path / "ce.zip", members,
        directories=("packed/", "packed/Cheat Engine/", "packed/Cheat Engine/autorun/"),
    )
    layout = inspect_ce_archive(source)
    stage = tmp_path / "stage"
    assert extract_ce_archive(source, layout, stage) == (layout.file_count, layout.total_bytes)
    # The sibling outside the installation root is not part of what was imported.
    assert not (stage / "readme.txt").exists()
    assert (stage / "cheatengine-x86_64.exe").is_file()
    assert (stage / "autorun" / "ceshare.lua").is_file()

    imported_root = tmp_path / "imported"
    promoted = promote_imported_installation(stage, imported_root, "a" * 64, layout)
    assert Path(promoted.root) == imported_root / promoted.sha256
    assert Path(promoted.executable).name == "cheatengine-x86_64.exe"
    assert validate_imported_installation(Path(promoted.root)).sha256 == promoted.sha256


def test_a_promoted_installation_whose_executable_changed_no_longer_validates(tmp_path: Path):
    source = _write_zip(tmp_path / "ce.zip", _installation(), directories=("autorun/",))
    layout = inspect_ce_archive(source)
    stage = tmp_path / "stage"
    extract_ce_archive(source, layout, stage)
    promoted = promote_imported_installation(stage, tmp_path / "imported", "b" * 64, layout)

    # Same length, different bytes: the manifest's file/byte totals still match,
    # so this exercises the executable identity check rather than tree drift.
    replacement = bytearray(_pe_bytes())
    replacement[0x400] = 0x41
    executable = Path(promoted.root) / "cheatengine-x86_64.exe"
    executable.write_bytes(bytes(replacement))
    with pytest.raises(ValueError, match="identity changed"):
        validate_imported_installation(Path(promoted.root))


def test_a_promoted_installation_without_its_manifest_no_longer_validates(tmp_path: Path):
    source = _write_zip(tmp_path / "ce.zip", _installation(), directories=("autorun/",))
    layout = inspect_ce_archive(source)
    stage = tmp_path / "stage"
    extract_ce_archive(source, layout, stage)
    promoted = promote_imported_installation(stage, tmp_path / "imported", "c" * 64, layout)

    (Path(promoted.root) / IMPORT_MANIFEST_NAME).unlink()
    with pytest.raises(ValueError, match="manifest is missing or invalid"):
        validate_imported_installation(Path(promoted.root))


def test_a_promoted_installation_with_tree_drift_no_longer_validates(tmp_path: Path):
    source = _write_zip(tmp_path / "ce.zip", _installation(), directories=("autorun/",))
    layout = inspect_ce_archive(source)
    stage = tmp_path / "stage"
    extract_ce_archive(source, layout, stage)
    promoted = promote_imported_installation(stage, tmp_path / "imported", "c" * 64, layout)

    (Path(promoted.root) / "unexpected.lua").write_text("return true\n", encoding="utf-8")
    with pytest.raises(ValueError, match="tree no longer matches"):
        validate_imported_installation(Path(promoted.root))


def test_archive_rejects_implicit_directory_tree_exhaustion(tmp_path: Path, monkeypatch):
    import ce_decky.ce_archive_import as archive_module

    source = _write_zip(tmp_path / "ce.zip", _installation(), directories=("autorun/",))
    monkeypatch.setattr(archive_module, "MAX_CE_ARCHIVE_TREE_ENTRIES", 10)
    with pytest.raises(ArchiveImportError, match="too many files and directories"):
        inspect_ce_archive(source)


@pytest.mark.parametrize("field,value", [
    ("archive_sha256", "not-a-digest"),
    ("file_count", True),
    ("total_bytes", 0),
])
def test_import_manifest_rejects_malformed_provenance_fields(tmp_path: Path, field: str, value: object):
    source = _write_zip(tmp_path / "ce.zip", _installation(), directories=("autorun/",))
    layout = inspect_ce_archive(source)
    stage = tmp_path / "stage"
    extract_ce_archive(source, layout, stage)
    promoted = promote_imported_installation(stage, tmp_path / "imported", "d" * 64, layout)
    manifest = Path(promoted.root) / IMPORT_MANIFEST_NAME
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload[field] = value
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="manifest is missing or invalid"):
        validate_imported_installation(Path(promoted.root))


def test_failed_replacement_restores_the_previous_install_before_cleanup(tmp_path: Path, monkeypatch):
    import ce_decky.ce_archive_import as archive_module

    first_members = _installation()
    first_members["bin0.dll"] = b"previous"
    source = _write_zip(tmp_path / "first.zip", first_members, directories=("autorun/",))
    layout = inspect_ce_archive(source)
    first_stage = tmp_path / "first-stage"
    extract_ce_archive(source, layout, first_stage)
    imported_root = tmp_path / "imported"
    first = promote_imported_installation(first_stage, imported_root, "e" * 64, layout)
    replacement_members = _installation()
    replacement_members["bin0.dll"] = b"replacement"
    replacement_source = _write_zip(tmp_path / "replacement.zip", replacement_members, directories=("autorun/",))
    replacement_layout = inspect_ce_archive(replacement_source)
    replacement_stage = tmp_path / "replacement-stage"
    extract_ce_archive(replacement_source, replacement_layout, replacement_stage)
    destination = Path(first.root)

    monkeypatch.setattr(archive_module, "validate_imported_installation", lambda _root: (_ for _ in ()).throw(ValueError("invalid")))

    def verify_then_discard(path: Path) -> None:
        assert (destination / "bin0.dll").read_bytes() == b"previous"
        shutil.rmtree(path)

    monkeypatch.setattr(archive_module, "_discard", verify_then_discard)
    with pytest.raises(ValueError, match="invalid"):
        promote_imported_installation(replacement_stage, imported_root, "f" * 64, replacement_layout)

    assert (destination / "bin0.dll").read_bytes() == b"previous"
