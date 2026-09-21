"""What a Steam game starts, what its folder holds, and what neither will answer."""
from pathlib import Path

import pytest

from ce_decky.game_files import MAX_DEPTH, MAX_EXECUTABLES, game_executables, program_in_tree, programs_in_tree
from tests.test_steam_launch import write_appinfo


def _library(home: Path, name: str = ".local/share/Steam") -> Path:
    library = home / name
    (library / "steamapps/common").mkdir(parents=True, exist_ok=True)
    return library


def _manifest(library: Path, app_id: int, installdir: str) -> None:
    (library / f"steamapps/appmanifest_{app_id}.acf").write_text(
        '"AppState"\n{\n'
        f'\t"appid"\t\t"{app_id}"\n'
        f'\t"name"\t\t"A Game"\n'
        f'\t"installdir"\t\t"{installdir}"\n'
        "}\n",
        encoding="utf-8",
    )


def _exe(path: Path, size: int = 16) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"M" * size)


def test_the_games_own_root_executable_is_found_and_named_first(tmp_path: Path):
    """The case this exists for, in the shape the device actually has it.

    Half-Life 2's install folder holds exactly `hl2.exe` at its root and thirty
    odd SDK compilers under `bin/`. Every one of those reads like a plausible
    program, so depth is what separates them and nothing about the names is.
    """
    home = tmp_path / "home"
    library = _library(home)
    _manifest(library, 220, "Half-Life 2")
    game = library / "steamapps/common/Half-Life 2"
    _exe(game / "hl2.exe", 127072)
    for tool in ("vrad.exe", "hammer.exe", "studiomdl.exe"):
        _exe(game / "bin" / tool)

    found = game_executables(home, 220)
    assert found["reason"] is None
    assert found["truncated"] is False
    assert found["install_dir"] == str(game.resolve())
    shallowest = found["executables"][0]
    assert shallowest["name"] == "hl2.exe"
    assert shallowest["directory"] == ""
    assert shallowest["depth"] == 0
    assert shallowest["size_bytes"] == 127072
    # The tools are reported too, with the directory that says what they are.
    assert {item["name"] for item in found["executables"][1:]} == {"vrad.exe", "hammer.exe", "studiomdl.exe"}
    assert all(item["directory"] == "bin" and item["depth"] == 1 for item in found["executables"][1:])


def test_an_absence_is_a_reason_rather_than_an_empty_list(tmp_path: Path):
    """An empty list reads as a game with no executables, which is a different fact."""
    home = tmp_path / "home"
    _library(home)
    missing = game_executables(home, 4242)
    assert missing["executables"] == []
    assert "no record of this game being installed" in str(missing["reason"])

    # Installed, but with nothing Windows in it, which a native Linux game is.
    library = _library(home)
    _manifest(library, 70, "Native")
    (library / "steamapps/common/Native").mkdir(parents=True)
    (library / "steamapps/common/Native/run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    native = game_executables(home, 70)
    assert native["executables"] == []
    assert "no Windows executable" in str(native["reason"])
    assert native["cause"] == "no_windows_executable"


def test_a_folder_that_could_not_be_read_is_never_proof_that_a_game_has_no_exe(tmp_path: Path):
    # `no_windows_executable` is the one answer a screen acts on: it hides the
    # process picker and refuses the press, on the ground that there is nothing
    # here to attach to. A directory this may not open could hold the one
    # executable, so an answer that skipped it says what it is instead of
    # claiming the game is a Linux build.
    home = tmp_path / "home"
    library = _library(home)
    _manifest(library, 71, "Native")
    game = library / "steamapps/common/Native"
    (game / "data").mkdir(parents=True)
    (game / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (game / "data").chmod(0o000)
    try:
        refused = game_executables(home, 71)
    finally:
        (game / "data").chmod(0o755)

    assert refused["executables"] == []
    assert refused["cause"] == "unreadable_files"
    assert "could not be read" in str(refused["reason"])
    # And the same install, readable, is the claim it always was.
    assert game_executables(home, 71)["cause"] == "no_windows_executable"


def test_one_game_in_two_libraries_is_refused_rather_than_chosen_between(tmp_path: Path):
    """The wrong one names a different build of the game, so neither is taken."""
    home = tmp_path / "home"
    first = _library(home)
    second = home / "Games/SteamLibrary"
    (second / "steamapps/common").mkdir(parents=True)
    (first / "steamapps/libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n}\n' % second,
        encoding="utf-8",
    )
    for library, name in ((first, "Game"), (second, "Game")):
        _manifest(library, 55, name)
        _exe(library / "steamapps/common" / name / "game.exe")

    found = game_executables(home, 55)
    assert found["executables"] == []
    assert "more than one Steam library" in str(found["reason"])


def test_a_manifest_that_names_another_app_or_escapes_its_library_is_refused(tmp_path: Path):
    """Acting on either would open a directory this has no business reading."""
    home = tmp_path / "home"
    library = _library(home)
    outside = home / "elsewhere"
    _exe(outside / "secret.exe")

    # A manifest naming a different AppID than its own file name is corruption.
    (library / "steamapps/appmanifest_11.acf").write_text(
        '"AppState"\n{\n\t"appid"\t\t"12"\n\t"installdir"\t\t"Game"\n}\n', encoding="utf-8",
    )
    _exe(library / "steamapps/common/Game/game.exe")
    assert game_executables(home, 11)["executables"] == []

    # And an install directory that is a path rather than one name is refused
    # rather than normalised into something that happens to exist.
    (library / "steamapps/appmanifest_12.acf").write_text(
        '"AppState"\n{\n\t"appid"\t\t"12"\n\t"installdir"\t\t"../../elsewhere"\n}\n', encoding="utf-8",
    )
    escaped = game_executables(home, 12)
    assert escaped["executables"] == []
    assert escaped["install_dir"] is None


def test_a_symlink_out_of_the_game_folder_is_not_followed(tmp_path: Path):
    """A game directory is user-writable, so a link in it is not a route out."""
    home = tmp_path / "home"
    library = _library(home)
    _manifest(library, 77, "Game")
    game = library / "steamapps/common/Game"
    _exe(game / "game.exe")
    outside = home / "elsewhere"
    _exe(outside / "secret.exe")
    (game / "link").symlink_to(outside, target_is_directory=True)
    (game / "linked.exe").symlink_to(outside / "secret.exe")

    names = {item["name"] for item in game_executables(home, 77)["executables"]}
    assert names == {"game.exe"}


def test_the_walk_is_bounded_by_depth_and_count_and_says_when_it_stopped(tmp_path: Path):
    home = tmp_path / "home"
    library = _library(home)
    _manifest(library, 88, "Game")
    game = library / "steamapps/common/Game"
    # An Unreal shipping binary is three deep and is reached; anything deeper is
    # a toolchain or a bundled runtime.
    _exe(game / "Binaries/Win64/Game-Win64-Shipping.exe")
    deep = game
    for level in range(MAX_DEPTH + 2):
        deep = deep / f"level{level}"
    _exe(deep / "buried.exe")
    names = {item["name"] for item in game_executables(home, 88)["executables"]}
    assert "Game-Win64-Shipping.exe" in names
    assert "buried.exe" not in names

    for index in range(MAX_EXECUTABLES + 5):
        _exe(game / f"tool{index:03d}.exe")
    many = game_executables(home, 88)
    assert len(many["executables"]) == MAX_EXECUTABLES
    assert many["truncated"] is True


def test_a_program_deeper_than_a_candidate_is_looked_for_is_not_an_absent_one(tmp_path: Path, monkeypatch):
    """Two questions of one tree, and only one of them stops at `MAX_DEPTH`.

    "Is there a candidate worth offering" is what three levels answers, because
    that is where a game's own executable is. "Is there a Windows program here
    at all" is what a screen refuses a press on, and reading the first as the
    second is how an install with its program one level further down would be
    called a native Linux build.
    """
    home = tmp_path / "home"
    library = _library(home)
    _manifest(library, 90, "Deep")
    game = library / "steamapps/common/Deep"
    buried = game
    for level in range(MAX_DEPTH + 1):
        buried = buried / f"level{level}"
    _exe(buried / "game.exe")

    deep = game_executables(home, 90)
    # Nothing is offered, because nothing worth offering was found - but the
    # install is not empty of Windows programs and does not say it is.
    assert deep["executables"] == []
    assert deep["cause"] == "executable_too_deep"
    assert "deeper than" in str(deep["reason"])


def test_the_deeper_question_is_only_asked_when_the_first_one_found_nothing(tmp_path: Path, monkeypatch):
    # The cost of answering it is a walk of the whole install, so it is not paid
    # for a game that already has a candidate.
    import os as _os

    home = tmp_path / "home"
    library = _library(home)
    _manifest(library, 91, "Shallow")
    game = library / "steamapps/common/Shallow"
    _exe(game / "game.exe")
    buried = game
    for level in range(MAX_DEPTH + 2):
        buried = buried / f"level{level}"
    (buried).mkdir(parents=True)

    read: list[str] = []
    real = _os.scandir
    monkeypatch.setattr(_os, "scandir", lambda path: (read.append(str(path)), real(path))[1])
    assert [item["name"] for item in game_executables(home, 91)["executables"]] == ["game.exe"]
    # The tree below the candidate depth was never opened.
    assert not [path for path in read if f"level{MAX_DEPTH}" in path]


def _native_install_with_a_deep_folder(tmp_path: Path, app_id: int) -> Path:
    home = tmp_path / "home"
    library = _library(home)
    _manifest(library, app_id, "Native")
    game = library / "steamapps/common/Native"
    deep = game
    for level in range(MAX_DEPTH + 1):
        deep = deep / f"level{level}"
    deep.mkdir(parents=True)
    (game / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    return deep


def test_a_deep_search_the_budget_stopped_is_not_proof_either(tmp_path: Path, monkeypatch):
    # The same rule as the candidate search: a bound is "nobody finished
    # looking", which is not an answer about the game.
    _native_install_with_a_deep_folder(tmp_path, 92)
    monkeypatch.setattr("ce_decky.game_files.MAX_ENTRIES", 2)
    assert game_executables(tmp_path / "home", 92)["cause"] == "too_many_files"


def test_a_deep_folder_that_could_not_be_opened_is_not_proof_either(tmp_path: Path):
    deep = _native_install_with_a_deep_folder(tmp_path, 93)
    deep.chmod(0o000)
    try:
        refused = game_executables(tmp_path / "home", 93)
    finally:
        deep.chmod(0o755)
    assert refused["cause"] == "unreadable_files"

    # And the same install, readable and holding nothing, is the claim it is.
    assert game_executables(tmp_path / "home", 93)["cause"] == "no_windows_executable"


def test_an_app_id_that_is_not_one_is_refused_rather_than_searched_for(tmp_path: Path):
    home = tmp_path / "home"
    _library(home)
    for value in (0, -1, True, "220"):
        with pytest.raises(ValueError):
            game_executables(home, value)  # type: ignore[arg-type]


def test_steams_own_record_answers_instead_of_the_folder(tmp_path: Path):
    """The whole point of this: one line from Steam rather than a folder of guesses.

    The same Half-Life 2 shape as the walk test above, with the record the
    client itself starts the game from. The SDK compilers are still on the disk
    and none of them is offered.
    """
    home = tmp_path / "home"
    library = _library(home)
    _manifest(library, 220, "Half-Life 2")
    game = library / "steamapps/common/Half-Life 2"
    _exe(game / "hl2.exe", 127072)
    for tool in ("vrad.exe", "hammer.exe", "studiomdl.exe"):
        _exe(game / "bin" / tool)
    write_appinfo(library, {220: {
        "config": {"launch": {"0": {
            "executable": "hl2.exe", "description": "Play",
            "config": {"oslist": "windows", "BetaKey": "default"},
        }}},
    }})

    found = game_executables(home, 220)
    assert found["source"] == "steam"
    assert found["reason"] is None
    assert found["declared_reason"] is None
    assert found["executables"] == [{
        "name": "hl2.exe", "directory": "", "depth": 0, "size_bytes": 127072,
        "declared": True, "description": "Play",
    }]


def test_the_branch_this_device_installed_is_the_branch_asked_about(tmp_path: Path):
    """One record holds every branch, and only one of them is about these files."""
    home = tmp_path / "home"
    library = _library(home)
    (library / "steamapps/appmanifest_220.acf").write_text(
        '"AppState"\n{\n\t"appid"\t\t"220"\n\t"installdir"\t\t"Half-Life 2"\n'
        '\t"UserConfig"\n\t{\n\t\t"BetaKey"\t\t"steam_legacy"\n\t}\n}\n',
        encoding="utf-8",
    )
    game = library / "steamapps/common/Half-Life 2"
    _exe(game / "hl2.exe")
    _exe(game / "hl2_legacy.exe")
    write_appinfo(library, {220: {"config": {"launch": {
        "0": {"executable": "hl2.exe", "config": {"oslist": "windows", "BetaKey": "default"}},
        "3": {"executable": "hl2_legacy.exe", "config": {"oslist": "windows", "BetaKey": "steam_legacy"}},
    }}}})

    found = game_executables(home, 220)
    assert [item["name"] for item in found["executables"]] == ["hl2_legacy.exe"]


def test_the_branch_on_the_disk_outranks_the_branch_that_was_asked_for(tmp_path: Path):
    """A branch that has not finished downloading is not the branch of these files."""
    home = tmp_path / "home"
    library = _library(home)
    (library / "steamapps/appmanifest_220.acf").write_text(
        '"AppState"\n{\n\t"appid"\t\t"220"\n\t"installdir"\t\t"Half-Life 2"\n'
        '\t"UserConfig"\n\t{\n\t\t"BetaKey"\t\t"test"\n\t}\n'
        '\t"MountedConfig"\n\t{\n\t\t"BetaKey"\t\t"steam_legacy"\n\t}\n}\n',
        encoding="utf-8",
    )
    game = library / "steamapps/common/Half-Life 2"
    _exe(game / "hl2_legacy.exe")
    _exe(game / "hl2_test.exe")
    write_appinfo(library, {220: {"config": {"launch": {
        "0": {"executable": "hl2_test.exe", "config": {"oslist": "windows", "BetaKey": "test"}},
        "3": {"executable": "hl2_legacy.exe", "config": {"oslist": "windows", "BetaKey": "steam_legacy"}},
    }}}})

    found = game_executables(home, 220)
    assert [item["name"] for item in found["executables"]] == ["hl2_legacy.exe"]


def test_a_declared_program_that_is_not_on_the_disk_falls_back_to_the_folder(tmp_path: Path):
    """A record about a game this device does not hold is not an answer about it.

    A branch switched under a cache Steam has not rewritten, and a depot that
    has not finished downloading, both look exactly like this.
    """
    home = tmp_path / "home"
    library = _library(home)
    _manifest(library, 220, "Half-Life 2")
    game = library / "steamapps/common/Half-Life 2"
    _exe(game / "hl2.exe")
    write_appinfo(library, {220: {"config": {"launch": {
        "0": {"executable": "not_here.exe", "config": {"oslist": "windows"}},
    }}}})

    found = game_executables(home, 220)
    assert found["source"] == "files"
    assert found["declared_reason"] == "the program Steam starts for this game is not in its installed files"
    assert [item["name"] for item in found["executables"]] == ["hl2.exe"]
    assert found["executables"][0]["declared"] is False


def test_a_windows_spelling_still_opens_on_a_case_sensitive_filesystem(tmp_path: Path):
    """Steam writes the depot's spelling, and a Linux filesystem is not Windows."""
    home = tmp_path / "home"
    library = _library(home)
    _manifest(library, 88, "Game")
    game = library / "steamapps/common/Game"
    _exe(game / "binaries/win64/game-win64-shipping.exe", 4096)
    write_appinfo(library, {88: {"config": {"launch": {
        "0": {"executable": "Binaries\\Win64\\Game-Win64-Shipping.exe", "config": {"oslist": "windows"}},
    }}}})

    found = game_executables(home, 88)
    assert found["source"] == "steam"
    assert [(item["name"], item["directory"], item["depth"]) for item in found["executables"]] == [
        ("Game-Win64-Shipping.exe", "Binaries/Win64", 2),
    ]


def test_a_declared_path_may_not_leave_the_game_folder(tmp_path: Path):
    """The same boundary the walk holds, against a record rather than a link."""
    home = tmp_path / "home"
    library = _library(home)
    _manifest(library, 77, "Game")
    game = library / "steamapps/common/Game"
    _exe(game / "game.exe")
    outside = home / "elsewhere"
    _exe(outside / "secret.exe")
    (game / "link").symlink_to(outside, target_is_directory=True)
    write_appinfo(library, {77: {"config": {"launch": {
        "0": {"executable": "link/secret.exe", "config": {"oslist": "windows"}},
    }}}})

    found = game_executables(home, 77)
    assert found["source"] == "files"
    assert [item["name"] for item in found["executables"]] == ["game.exe"]


def test_a_walk_that_did_not_finish_says_so_rather_than_answering(tmp_path: Path):
    """One file found is the only one there is, or the only one this got to.

    The walk is bounded in depth and in entries, and a caller that removes code
    from a table because a pattern is absent from the file it picked has to be
    able to tell those apart. A name that picks out one file in a walk that
    still had directories to look in is not an identity.
    """
    root = tmp_path / "game"
    (root / "win64").mkdir(parents=True)
    (root / "win64" / "engine.dll").write_bytes(b"\x00")

    found, complete = programs_in_tree(root, "engine.dll")
    assert [item.name for item in found] == ["engine.dll"] and complete is True

    # A second copy deeper than the walk is allowed to go, and the walk says it
    # did not finish rather than reporting the one it reached as the only one.
    deep = root
    for level in range(MAX_DEPTH + 2):
        deep = deep / f"level{level}"
    deep.mkdir(parents=True)
    (deep / "engine.dll").write_bytes(b"\x11")

    found, complete = programs_in_tree(root, "engine.dll")
    assert [item.name for item in found] == ["engine.dll"] and complete is False
    # And the first-match helper still answers what it always answered.
    assert program_in_tree(root, "engine.dll") == root / "win64" / "engine.dll"


def test_two_files_of_one_name_are_both_reported(tmp_path: Path):
    """Which of them a game loads is not something a listing can say."""
    root = tmp_path / "game"
    (root / "x64").mkdir(parents=True)
    (root / "x86").mkdir(parents=True)
    (root / "x64" / "engine.dll").write_bytes(b"\x00")
    (root / "x86" / "engine.dll").write_bytes(b"\x11")

    found, complete = programs_in_tree(root, "engine.dll")
    assert len(found) == 2 and complete is True


def test_a_subtree_this_could_not_read_is_not_a_finished_walk(tmp_path: Path):
    """A directory nobody could list may hold the second file of that name.

    The answer is used to decide whether one file is the only one there is, and
    a walk that says it finished has to have finished.
    """
    root = tmp_path / "game"
    (root / "win64").mkdir(parents=True)
    (root / "win64" / "engine.dll").write_bytes(b"\x00")
    refused = root / "refused"
    refused.mkdir()
    (refused / "engine.dll").write_bytes(b"\x11")
    refused.chmod(0o000)
    try:
        found, complete = programs_in_tree(root, "engine.dll")
    finally:
        refused.chmod(0o755)

    assert [item.name for item in found] == ["engine.dll"]
    assert complete is False
