from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import os

import pytest

import ce_decky.ce_runtime as ce_runtime
from ce_decky.ce_runtime import BRIDGE_NAME, materialize_private_runtime


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _tree(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    source = tmp_path / "source-ce"
    source.mkdir()
    exe = source / "Cheat Engine.exe"
    exe.write_bytes(b"fake-ce-binary-for-materialization")
    (source / "main.lua").write_text("require('defines')\n")
    (source / "autorun").mkdir()
    (source / "autorun" / "upstream.lua").write_text("print('upstream')\n")
    (source / "languages").mkdir()
    (source / "languages" / "x.txt").write_text("x")
    bridge = tmp_path / "bridge.lua"
    bridge.write_text("print('ce-decky')\n")
    return source, exe, bridge, _digest(exe)


def test_materializes_private_immutable_runtime_without_touching_source(tmp_path: Path):
    source, exe, bridge, source_sha = _tree(tmp_path)
    managed = tmp_path / "managed-ce"

    result = materialize_private_runtime(
        source_root=source,
        source_executable=exe,
        source_executable_sha256=source_sha,
        managed_ce_root=managed,
        bridge_source=bridge,
    )

    runtime = Path(result.root)
    assert runtime.is_dir()
    assert Path(result.executable).read_bytes() == exe.read_bytes()
    assert (runtime / "autorun" / BRIDGE_NAME).read_text() == bridge.read_text()
    assert (runtime / ce_runtime.SOURCE_MAIN_NAME).read_text() == "require('defines')\n"
    assert (runtime / ce_runtime.MAIN_NAME).read_bytes() == ce_runtime._MAIN_BOOTSTRAP
    assert not (source / "autorun" / BRIDGE_NAME).exists()
    assert result.source_executable_sha256 == source_sha
    assert runtime.parent == managed / "runtime"
    assert len(runtime.name) == 64

    # Same source+bridge is idempotent and uses the immutable existing tree.
    again = materialize_private_runtime(
        source_root=source,
        source_executable=exe,
        source_executable_sha256=source_sha,
        managed_ce_root=managed,
        bridge_source=bridge,
    )
    assert again == result


def test_rejects_symlink_in_source_tree(tmp_path: Path):
    source, exe, bridge, source_sha = _tree(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    try:
        os.symlink(outside, source / "escape")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")

    with pytest.raises(ValueError, match="symlink"):
        materialize_private_runtime(
            source_root=source,
            source_executable=exe,
            source_executable_sha256=source_sha,
            managed_ce_root=tmp_path / "managed",
            bridge_source=bridge,
        )


def test_rejects_source_sha_drift(tmp_path: Path):
    source, exe, bridge, source_sha = _tree(tmp_path)
    exe.write_bytes(exe.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="SHA changed"):
        materialize_private_runtime(
            source_root=source,
            source_executable=exe,
            source_executable_sha256=source_sha,
            managed_ce_root=tmp_path / "managed",
            bridge_source=bridge,
        )


def test_rejects_unbounded_source_tree(tmp_path: Path):
    source, exe, bridge, source_sha = _tree(tmp_path)
    with pytest.raises(ValueError, match="file-count|entry limit"):
        materialize_private_runtime(
            source_root=source,
            source_executable=exe,
            source_executable_sha256=source_sha,
            managed_ce_root=tmp_path / "managed",
            bridge_source=bridge,
            max_files=1,
        )


def test_rejects_unbounded_empty_directories_before_sorting_the_tree(tmp_path: Path):
    root = tmp_path / "directories"
    root.mkdir()
    for index in range(3):
        (root / str(index)).mkdir()

    with pytest.raises(ValueError, match="entry limit"):
        ce_runtime._bounded_tree_paths(root, max_entries=2)


def test_refuses_to_overwrite_tampered_immutable_runtime(tmp_path: Path):
    source, exe, bridge, source_sha = _tree(tmp_path)
    managed = tmp_path / "managed"
    result = materialize_private_runtime(
        source_root=source,
        source_executable=exe,
        source_executable_sha256=source_sha,
        managed_ce_root=managed,
        bridge_source=bridge,
    )
    (Path(result.root) / "autorun" / BRIDGE_NAME).write_text("tampered")
    with pytest.raises(ValueError, match="no longer matches its verified snapshot"):
        materialize_private_runtime(
            source_root=source,
            source_executable=exe,
            source_executable_sha256=source_sha,
            managed_ce_root=managed,
            bridge_source=bridge,
        )
    assert (Path(result.root) / "autorun" / BRIDGE_NAME).read_text() == "tampered"


def test_rebuilds_runtime_cheat_engine_dirtied_during_its_own_run(tmp_path: Path):
    """CE writes ``autorun/ceshare/processlist.txt`` into its own directory."""
    source, exe, bridge, source_sha = _tree(tmp_path)
    managed = tmp_path / "managed"
    first = materialize_private_runtime(
        source_root=source,
        source_executable=exe,
        source_executable_sha256=source_sha,
        managed_ce_root=managed,
        bridge_source=bridge,
    )
    written = Path(first.root) / "autorun" / "ceshare"
    written.mkdir()
    (written / "processlist.txt").write_text("74a54d29c321bd7298fd4de3e46f26b0\n")

    second = materialize_private_runtime(
        source_root=source,
        source_executable=exe,
        source_executable_sha256=source_sha,
        managed_ce_root=managed,
        bridge_source=bridge,
        allow_replace=True,
    )

    assert second == first
    assert not (written / "processlist.txt").exists()
    assert (Path(second.root) / "autorun" / BRIDGE_NAME).read_text() == bridge.read_text()
    assert list((managed / "runtime" / ".staging").iterdir()) == []
    # The rebuilt tree validates again without another replacement.
    assert materialize_private_runtime(
        source_root=source,
        source_executable=exe,
        source_executable_sha256=source_sha,
        managed_ce_root=managed,
        bridge_source=bridge,
    ) == first


def test_replacement_restores_a_tampered_runtime_to_verified_content(tmp_path: Path):
    source, exe, bridge, source_sha = _tree(tmp_path)
    managed = tmp_path / "managed"
    first = materialize_private_runtime(
        source_root=source,
        source_executable=exe,
        source_executable_sha256=source_sha,
        managed_ce_root=managed,
        bridge_source=bridge,
    )
    (Path(first.root) / "autorun" / BRIDGE_NAME).write_text("tampered")

    second = materialize_private_runtime(
        source_root=source,
        source_executable=exe,
        source_executable_sha256=source_sha,
        managed_ce_root=managed,
        bridge_source=bridge,
        allow_replace=True,
    )

    assert second == first
    assert (Path(second.root) / "autorun" / BRIDGE_NAME).read_text() == bridge.read_text()


def test_failed_replacement_restores_the_previous_runtime_tree(tmp_path: Path, monkeypatch):
    source, exe, bridge, source_sha = _tree(tmp_path)
    managed = tmp_path / "managed"
    first = materialize_private_runtime(
        source_root=source,
        source_executable=exe,
        source_executable_sha256=source_sha,
        managed_ce_root=managed,
        bridge_source=bridge,
    )
    marker = Path(first.root) / "autorun" / "ceshare-processlist.txt"
    marker.write_text("dirty")

    real_rename = os.rename
    calls: list[tuple[str, str]] = []

    def failing_rename(src, dst, *args, **kwargs):
        calls.append((str(src), str(dst)))
        if str(dst) == first.root and Path(src).name.startswith("retired."):
            return real_rename(src, dst, *args, **kwargs)
        if str(dst) == first.root:
            raise OSError("simulated promotion failure")
        return real_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(ce_runtime.os, "rename", failing_rename)
    with pytest.raises(OSError, match="simulated promotion failure"):
        materialize_private_runtime(
            source_root=source,
            source_executable=exe,
            source_executable_sha256=source_sha,
            managed_ce_root=managed,
            bridge_source=bridge,
            allow_replace=True,
        )

    assert marker.read_text() == "dirty"
    assert (Path(first.root) / "autorun" / BRIDGE_NAME).read_text() == bridge.read_text()


def test_requires_ce_like_source_root_with_autorun_directory(tmp_path: Path):
    source = tmp_path / "random-folder"
    source.mkdir()
    exe = source / "Cheat Engine.exe"
    exe.write_bytes(b"fake-ce-binary-for-materialization")
    bridge = tmp_path / "bridge.lua"
    bridge.write_text("print('bridge')")
    with pytest.raises(ValueError, match="autorun"):
        materialize_private_runtime(
            source_root=source,
            source_executable=exe,
            source_executable_sha256=_digest(exe),
            managed_ce_root=tmp_path / "managed",
            bridge_source=bridge,
        )


def test_runtime_identity_includes_support_files(tmp_path: Path):
    source, exe, bridge, source_sha = _tree(tmp_path)
    managed = tmp_path / "managed"
    first = materialize_private_runtime(
        source_root=source,
        source_executable=exe,
        source_executable_sha256=source_sha,
        managed_ce_root=managed,
        bridge_source=bridge,
    )
    (source / "languages" / "x.txt").write_text("changed-support-file")
    second = materialize_private_runtime(
        source_root=source,
        source_executable=exe,
        source_executable_sha256=source_sha,
        managed_ce_root=managed,
        bridge_source=bridge,
    )
    assert first.source_executable_sha256 == second.source_executable_sha256
    assert first.source_tree_sha256 != second.source_tree_sha256
    assert first.root != second.root


def test_existing_runtime_detects_support_file_tamper(tmp_path: Path):
    source, exe, bridge, source_sha = _tree(tmp_path)
    managed = tmp_path / "managed"
    result = materialize_private_runtime(
        source_root=source,
        source_executable=exe,
        source_executable_sha256=source_sha,
        managed_ce_root=managed,
        bridge_source=bridge,
    )
    (Path(result.root) / "languages" / "x.txt").write_text("tampered")
    with pytest.raises(ValueError, match="no longer matches its verified snapshot"):
        materialize_private_runtime(
            source_root=source,
            source_executable=exe,
            source_executable_sha256=source_sha,
            managed_ce_root=managed,
            bridge_source=bridge,
        )


def test_reserved_bridge_name_in_source_is_rejected(tmp_path: Path):
    source, exe, bridge, source_sha = _tree(tmp_path)
    (source / "autorun" / BRIDGE_NAME).write_text("user-owned")
    with pytest.raises(ValueError, match="reserved runtime path"):
        materialize_private_runtime(
            source_root=source,
            source_executable=exe,
            source_executable_sha256=source_sha,
            managed_ce_root=tmp_path / "managed",
            bridge_source=bridge,
        )


def test_legacy_bridge_name_in_source_is_still_rejected(tmp_path: Path):
    source, exe, bridge, source_sha = _tree(tmp_path)
    (source / "autorun" / ce_runtime.LEGACY_BRIDGE_NAME).write_text("user-owned")
    with pytest.raises(ValueError, match="reserved runtime path"):
        materialize_private_runtime(
            source_root=source,
            source_executable=exe,
            source_executable_sha256=source_sha,
            managed_ce_root=tmp_path / "managed",
            bridge_source=bridge,
        )


def test_reserved_preserved_main_name_in_source_is_rejected(tmp_path: Path):
    source, exe, bridge, source_sha = _tree(tmp_path)
    (source / ce_runtime.SOURCE_MAIN_NAME).write_text("user-owned")
    with pytest.raises(ValueError, match="reserved runtime path"):
        materialize_private_runtime(
            source_root=source,
            source_executable=exe,
            source_executable_sha256=source_sha,
            managed_ce_root=tmp_path / "managed",
            bridge_source=bridge,
        )


@pytest.mark.parametrize("relative", [ce_runtime.MAIN_NAME, ce_runtime.SOURCE_MAIN_NAME])
def test_existing_runtime_detects_main_bootstrap_tamper(tmp_path: Path, relative: str):
    source, exe, bridge, source_sha = _tree(tmp_path)
    managed = tmp_path / "managed"
    result = materialize_private_runtime(
        source_root=source,
        source_executable=exe,
        source_executable_sha256=source_sha,
        managed_ce_root=managed,
        bridge_source=bridge,
    )
    (Path(result.root) / relative).write_text("tampered")
    with pytest.raises(ValueError, match="no longer matches its verified snapshot"):
        materialize_private_runtime(
            source_root=source,
            source_executable=exe,
            source_executable_sha256=source_sha,
            managed_ce_root=managed,
            bridge_source=bridge,
        )


def test_detects_source_tree_change_during_snapshot_copy(tmp_path: Path, monkeypatch):
    source, exe, bridge, source_sha = _tree(tmp_path)
    original_copy = ce_runtime._copy_one_regular_file
    mutated = False

    def racing_copy(src: Path, target: Path, *, byte_budget: int) -> int:
        nonlocal mutated
        if not mutated:
            mutated = True
            (source / "languages" / "x.txt").write_text("changed during copy")
        return original_copy(src, target, byte_budget=byte_budget)

    monkeypatch.setattr(ce_runtime, "_copy_one_regular_file", racing_copy)
    with pytest.raises(ValueError, match="changed while creating"):
        materialize_private_runtime(
            source_root=source,
            source_executable=exe,
            source_executable_sha256=source_sha,
            managed_ce_root=tmp_path / "managed",
            bridge_source=bridge,
        )


def test_existing_runtime_detects_directory_symlink_tamper(tmp_path: Path):
    source, exe, bridge, source_sha = _tree(tmp_path)
    managed = tmp_path / "managed"
    result = materialize_private_runtime(
        source_root=source,
        source_executable=exe,
        source_executable_sha256=source_sha,
        managed_ce_root=managed,
        bridge_source=bridge,
    )
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    try:
        os.symlink(outside, Path(result.root) / "tampered-dir")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")

    with pytest.raises(ValueError, match="no longer matches its verified snapshot"):
        materialize_private_runtime(
            source_root=source,
            source_executable=exe,
            source_executable_sha256=source_sha,
            managed_ce_root=managed,
            bridge_source=bridge,
        )


def test_rejects_managed_ce_root_symlink_redirection(tmp_path: Path):
    source, exe, bridge, source_sha = _tree(tmp_path)
    outside = tmp_path / "outside-managed"
    outside.mkdir()
    managed = tmp_path / "managed-link"
    managed.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="managed CE root.*symlink"):
        materialize_private_runtime(
            source_root=source,
            source_executable=exe,
            source_executable_sha256=source_sha,
            managed_ce_root=managed,
            bridge_source=bridge,
        )


def test_existing_runtime_rejects_symlinked_manifest(tmp_path: Path):
    source, exe, bridge, source_sha = _tree(tmp_path)
    managed = tmp_path / "managed"
    result = materialize_private_runtime(
        source_root=source,
        source_executable=exe,
        source_executable_sha256=source_sha,
        managed_ce_root=managed,
        bridge_source=bridge,
    )
    runtime = Path(result.root)
    manifest = runtime / ce_runtime.MANIFEST_NAME
    outside = tmp_path / "manifest.json"
    outside.write_bytes(manifest.read_bytes())
    manifest.unlink()
    manifest.symlink_to(outside)
    with pytest.raises(ValueError, match="no longer matches its verified snapshot"):
        materialize_private_runtime(
            source_root=source,
            source_executable=exe,
            source_executable_sha256=source_sha,
            managed_ce_root=managed,
            bridge_source=bridge,
        )


def test_runtime_rejects_windows_case_and_trailing_dot_collisions(tmp_path: Path):
    source_root, executable, bridge, source_sha = _tree(tmp_path)
    managed = tmp_path / "managed"
    (source_root / "data").mkdir()
    (source_root / "data" / "Thing.dll").write_bytes(b"one")
    (source_root / "data" / "thing.DLL").write_bytes(b"two")
    with pytest.raises(ValueError, match="colliding"):
        materialize_private_runtime(
            source_root=source_root,
            source_executable=executable,
            source_executable_sha256=source_sha,
            managed_ce_root=managed,
            bridge_source=bridge,
        )

    (source_root / "data" / "thing.DLL").unlink()
    (source_root / "data" / "Thing.dll.").write_bytes(b"two")
    with pytest.raises(ValueError, match="colliding"):
        materialize_private_runtime(
            source_root=source_root,
            source_executable=executable,
            source_executable_sha256=source_sha,
            managed_ce_root=managed,
            bridge_source=bridge,
        )


def test_runtime_rejects_windows_reserved_and_case_variant_bridge_paths(tmp_path: Path):
    source_root, executable, bridge, source_sha = _tree(tmp_path)
    managed = tmp_path / "managed"
    (source_root / "CON.txt").write_bytes(b"bad")
    with pytest.raises(ValueError, match="reserved Windows"):
        materialize_private_runtime(
            source_root=source_root,
            source_executable=executable,
            source_executable_sha256=source_sha,
            managed_ce_root=managed,
            bridge_source=bridge,
        )
    (source_root / "CON.txt").unlink()
    (source_root / "autorun" / "CE_DECKY_BRIDGE.LUA").write_bytes(b"collision")
    with pytest.raises(ValueError, match="reserved runtime path"):
        materialize_private_runtime(
            source_root=source_root,
            source_executable=executable,
            source_executable_sha256=source_sha,
            managed_ce_root=managed,
            bridge_source=bridge,
        )


def test_rejects_symlinked_managed_runtime_component(tmp_path: Path):
    source, exe, bridge, source_sha = _tree(tmp_path)
    managed = tmp_path / "managed"
    managed.mkdir()
    outside = tmp_path / "outside-runtime"
    outside.mkdir()
    (managed / "runtime").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="managed CE directory"):
        materialize_private_runtime(
            source_root=source,
            source_executable=exe,
            source_executable_sha256=source_sha,
            managed_ce_root=managed,
            bridge_source=bridge,
        )


def test_stale_private_runtimes_are_collected_but_the_current_one_is_kept(tmp_path: Path):
    """A changed bridge or reinstalled CE leaves a sibling of installation size.

    The runtime key binds executable SHA, source-tree SHA and bridge SHA, so an
    ordinary plugin update materializes a new directory beside the old one.
    Nothing collected them, so each update leaked roughly one Cheat Engine tree.
    """
    import json

    from ce_decky.ce_runtime import MANIFEST_NAME, _runtime_key, collect_stale_runtimes

    managed = tmp_path / "ce"
    runtimes = managed / "runtime"

    def place(executable_sha: str, tree_sha: str, bridge_sha: str) -> str:
        key = _runtime_key(executable_sha, tree_sha, bridge_sha)
        (runtimes / key).mkdir(parents=True)
        (runtimes / key / MANIFEST_NAME).write_text(json.dumps({
            "source_executable_sha256": executable_sha,
            "source_tree_sha256": tree_sha,
            "bridge_sha256": bridge_sha,
        }), encoding="utf-8")
        (runtimes / key / "cheatengine-x86_64.exe").write_bytes(b"MZ")
        return key

    current = place("a" * 64, "b" * 64, "c" * 64)
    stale_a = place("a" * 64, "b" * 64, "d" * 64)
    stale_b = place("a" * 64, "e" * 64, "c" * 64)

    # No manifest at all: reported elsewhere as corrupt state, never deleted.
    (runtimes / "foreign").mkdir()
    (runtimes / "foreign" / "keep.txt").write_text("keep", encoding="utf-8")
    # A directory that merely contains a file with the reserved name, and one
    # whose manifest does not name it. A filename is not an identity.
    (runtimes / "impostor").mkdir()
    (runtimes / "impostor" / MANIFEST_NAME).write_text("not json", encoding="utf-8")
    (runtimes / "impostor" / "keep.txt").write_text("keep", encoding="utf-8")
    (runtimes / "mislabelled").mkdir()
    (runtimes / "mislabelled" / MANIFEST_NAME).write_text(json.dumps({
        "source_executable_sha256": "a" * 64, "source_tree_sha256": "b" * 64, "bridge_sha256": "c" * 64,
    }), encoding="utf-8")
    (runtimes / "mislabelled" / "keep.txt").write_text("keep", encoding="utf-8")

    removed = collect_stale_runtimes(managed, current)

    assert sorted(removed) == sorted([stale_a, stale_b])
    assert (runtimes / current / "cheatengine-x86_64.exe").is_file()
    assert (runtimes / "foreign" / "keep.txt").is_file()
    assert (runtimes / "impostor" / "keep.txt").is_file()
    assert (runtimes / "mislabelled" / "keep.txt").is_file()
    assert not (runtimes / stale_a).exists()
    assert collect_stale_runtimes(managed, current) == []
    assert collect_stale_runtimes(tmp_path / "missing", current) == []
