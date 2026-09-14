from __future__ import annotations

from pathlib import Path

import pytest

from scripts import target_prefix_probe


def _prefix(library: Path, app_id: int) -> Path:
    prefix = library / "steamapps" / "compatdata" / str(app_id) / "pfx"
    cmd = prefix / "drive_c" / "windows" / "system32" / "cmd.exe"
    cmd.parent.mkdir(parents=True)
    cmd.write_bytes(b"fixture")
    return prefix


def _library_vdf(primary: Path, secondary: Path) -> None:
    steamapps = primary / "steamapps"
    steamapps.mkdir(parents=True, exist_ok=True)
    encoded = str(secondary).replace("\\", "\\\\")
    (steamapps / "libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n  "1"\n  {\n    "path" "' + encoded + '"\n  }\n}\n',
        encoding="utf-8",
    )


def test_resolves_exact_appid_from_primary_library(tmp_path: Path):
    home = tmp_path / "home"
    library = home / ".local" / "share" / "Steam"
    prefix = _prefix(library, 123)

    report = target_prefix_probe.resolve_prefix(123, home)
    assert report["schema"] == 2
    assert report["ok"] is True
    assert report["state"] == "resolved"
    assert report["selected_prefix"] == str(prefix.resolve())
    assert report["prefix_cmd_exe"].endswith("cmd.exe")
    assert report["unsafe_exact_paths"] == []
    assert report["unavailable_library_paths"] == []


def test_discovers_secondary_library_from_libraryfolders_vdf(tmp_path: Path):
    home = tmp_path / "home"
    steam = home / ".local" / "share" / "Steam"
    secondary = tmp_path / "Secondary Library"
    _prefix(secondary, 456)
    _library_vdf(steam, secondary)

    report = target_prefix_probe.resolve_prefix(456, home)
    assert report["ok"] is True
    assert report["selected_prefix"] == str((secondary / "steamapps" / "compatdata" / "456" / "pfx").resolve())
    assert any("libraryfolders.vdf" in source for source in report["library_sources"])
    assert report["unavailable_library_paths"] == []


def test_multiple_exact_prefixes_are_ambiguous(tmp_path: Path):
    home = tmp_path / "home"
    primary = home / ".local" / "share" / "Steam"
    secondary = tmp_path / "secondary"
    _prefix(primary, 789)
    _prefix(secondary, 789)
    _library_vdf(primary, secondary)

    report = target_prefix_probe.resolve_prefix(789, home)
    assert report["ok"] is False
    assert report["state"] == "ambiguous"
    assert report["selected_prefix"] is None
    assert len(report["candidates"]) == 2


def test_unavailable_declared_library_blocks_otherwise_unique_candidate(tmp_path: Path):
    home = tmp_path / "home"
    primary = home / ".local" / "share" / "Steam"
    missing_secondary = tmp_path / "unmounted-library"
    _prefix(primary, 788)
    _library_vdf(primary, missing_secondary)

    report = target_prefix_probe.resolve_prefix(788, home)
    assert report["ok"] is False
    assert report["state"] == "unsafe"
    assert report["selected_prefix"] is None
    assert len(report["candidates"]) == 1
    assert report["unavailable_library_paths"] == [str(missing_secondary)]


def test_unsafe_exact_prefix_blocks_otherwise_unique_candidate(tmp_path: Path):
    home = tmp_path / "home"
    primary = home / ".local" / "share" / "Steam"
    secondary = tmp_path / "secondary"
    _prefix(primary, 790)
    target = tmp_path / "outside-prefix"
    target.mkdir()
    unsafe = secondary / "steamapps" / "compatdata" / "790" / "pfx"
    unsafe.parent.mkdir(parents=True)
    try:
        unsafe.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    _library_vdf(primary, secondary)

    report = target_prefix_probe.resolve_prefix(790, home)
    assert report["ok"] is False
    assert report["state"] == "unsafe"
    assert report["selected_prefix"] is None
    assert len(report["candidates"]) == 1
    assert report["unsafe_exact_paths"] == [str(unsafe)]


def test_intermediate_appid_symlink_is_unsafe_instead_of_resolving_outside_library(tmp_path: Path):
    home = tmp_path / "home"
    library = home / ".local" / "share" / "Steam"
    app_root = library / "steamapps" / "compatdata" / "791"
    app_root.parent.mkdir(parents=True)
    outside = tmp_path / "outside-appid"
    _prefix(outside, 1)
    outside_target = outside / "steamapps" / "compatdata" / "1"
    try:
        app_root.symlink_to(outside_target, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")

    report = target_prefix_probe.resolve_prefix(791, home)
    assert report["ok"] is False
    assert report["state"] == "unsafe"
    assert report["selected_prefix"] is None
    assert report["candidates"] == []
    assert report["unsafe_exact_paths"] == [str(app_root)]


def test_symlinked_libraryfolders_vdf_fails_closed(tmp_path: Path):
    home = tmp_path / "home"
    primary = home / ".local" / "share" / "Steam"
    steamapps = primary / "steamapps"
    steamapps.mkdir(parents=True)
    real = tmp_path / "libraryfolders.vdf"
    real.write_text('"libraryfolders"\n{\n}\n', encoding="utf-8")
    link = steamapps / "libraryfolders.vdf"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(ValueError, match="must not be a symlink"):
        target_prefix_probe.resolve_prefix(123, home)


def test_malformed_library_path_line_fails_closed(tmp_path: Path):
    home = tmp_path / "home"
    steamapps = home / ".local" / "share" / "Steam" / "steamapps"
    steamapps.mkdir(parents=True)
    (steamapps / "libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n  "path" broken\n}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="malformed library path line"):
        target_prefix_probe.resolve_prefix(123, home)


def test_missing_exact_appid_never_selects_neighbor(tmp_path: Path):
    home = tmp_path / "home"
    library = home / ".local" / "share" / "Steam"
    _prefix(library, 1000)
    report = target_prefix_probe.resolve_prefix(1001, home)
    assert report["ok"] is False
    assert report["state"] == "missing"
    assert report["candidates"] == []


def test_rejects_invalid_appid(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    for value in (0, -1, 0x100000000):
        with pytest.raises(ValueError, match="AppID"):
            target_prefix_probe.resolve_prefix(value, home)


def test_cli_requires_explicit_decky_user_home():
    parser = target_prefix_probe._parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--appid", "123"])
    args = parser.parse_args(["--appid", "123", "--home", "/home/deck"])
    assert args.appid == 123
    assert args.home == Path("/home/deck")
