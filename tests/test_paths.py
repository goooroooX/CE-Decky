from pathlib import Path

from ce_decky.paths import PluginPaths


def test_test_paths_keep_proton_artifacts_under_user_home(tmp_path: Path):
    paths = PluginPaths.for_tests(tmp_path)
    assert paths.managed_root == paths.user_home / ".cheat-engine-decky"
    assert paths.tables_root.is_relative_to(paths.managed_root)
    assert paths.temp_root.is_relative_to(paths.managed_root)
    paths.ensure()
    assert paths.temp_root.is_dir()


def test_managed_paths_reject_symlink_redirection(tmp_path: Path):
    paths = PluginPaths.for_tests(tmp_path)
    paths.user_home.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    paths.managed_root.symlink_to(outside, target_is_directory=True)
    try:
        paths.ensure()
    except RuntimeError as exc:
        assert "symlink" in str(exc)
    else:
        raise AssertionError("managed-root symlink redirection was accepted")


def test_managed_paths_reject_non_directory_collision(tmp_path: Path):
    paths = PluginPaths.for_tests(tmp_path)
    paths.user_home.mkdir(parents=True)
    paths.managed_root.write_text("not a directory")
    try:
        paths.ensure()
    except RuntimeError as exc:
        assert "not a directory" in str(exc)
    else:
        raise AssertionError("managed-root file collision was accepted")


def test_managed_root_symlink_escape_is_rejected(tmp_path: Path):
    paths = PluginPaths.for_tests(tmp_path)
    paths.user_home.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    paths.managed_root.symlink_to(outside, target_is_directory=True)
    with __import__("pytest").raises(RuntimeError, match="symlink"):
        paths.ensure()


def test_managed_subdirectory_symlink_escape_is_rejected(tmp_path: Path):
    paths = PluginPaths.for_tests(tmp_path)
    paths.managed_root.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    paths.tables_root.symlink_to(outside, target_is_directory=True)
    with __import__("pytest").raises(RuntimeError, match="symlink"):
        paths.ensure()
