from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from hashlib import sha256
import hashlib
import json
import os
import shutil
from pathlib import Path
import time
import zipfile

import pytest

from ce_decky import frontend_journal
from ce_decky.atomic import atomic_write_bytes
from scripts import target_plugin_install


class FakeWebSocket:
    def __init__(self, messages: list[dict[str, object]]) -> None:
        self.messages = deque(messages)
        self.sent: list[dict[str, object]] = []

    def send_json(self, value: dict[str, object]) -> None:
        self.sent.append(value)

    def receive_json(self) -> dict[str, object]:
        return self.messages.popleft()


def test_install_waits_for_confirm_reply_after_early_request_reply() -> None:
    ws = FakeWebSocket([
        {"type": 1, "id": 1, "result": None},
        {
            "type": 3,
            "event": "loader/add_plugin_install_prompt",
            "args": ["CE Decky", "0.5.1", "request-id", "a" * 64, 0],
        },
        {"type": 1, "id": 2, "result": None},
    ])

    target_plugin_install.install_and_confirm(ws, "http://127.0.0.1/plugin.zip", "0.5.1", "a" * 64, False)

    assert ws.sent[0]["route"] == "utilities/install_plugin"
    assert ws.sent[1] == {
        "type": 0,
        "route": "utilities/confirm_plugin_install",
        "args": ["request-id"],
        "id": 2,
    }


def test_install_confirms_a_prompt_pushed_in_the_newer_event_envelope() -> None:
    """Decky v3.2.8-pre1 pushes this event as type 5, v3.2.6 pushed it as type 3.

    The prompt carries the only confirmation id there is, so an unrecognised
    envelope is not a loud failure: the helper waits for a message that has
    already gone past, Decky closes the socket, and nothing is installed. That
    is exactly what the pre-release loader did on the target.
    """
    ws = FakeWebSocket([
        {
            "type": 5,
            "event": "loader/add_plugin_install_prompt",
            "args": ["CE Decky", "0.5.1", "request-id", "a" * 64, 4],
        },
        {"type": 1, "id": 1, "result": None},
        {"type": 1, "id": 2, "result": None},
    ])

    target_plugin_install.install_and_confirm(ws, "http://127.0.0.1/plugin.zip", "0.5.1", "a" * 64, True)

    assert ws.sent[1] == {
        "type": 0,
        "route": "utilities/confirm_plugin_install",
        "args": ["request-id"],
        "id": 2,
    }


def test_install_surfaces_confirm_failure_instead_of_accepting_early_reply() -> None:
    ws = FakeWebSocket([
        {"type": 1, "id": 1, "result": None},
        {
            "type": 3,
            "event": "loader/add_plugin_install_prompt",
            "args": ["CE Decky", "0.5.1", "request-id", "a" * 64, 4],
        },
        {"type": -1, "id": 2, "error": {"name": "DownloadError", "message": "artifact disappeared"}},
    ])

    with pytest.raises(RuntimeError, match="artifact disappeared"):
        target_plugin_install.install_and_confirm(
            ws, "http://127.0.0.1/plugin.zip", "0.5.1", "a" * 64, True
        )


def test_install_rejects_confirmation_for_another_plugin() -> None:
    ws = FakeWebSocket([
        {
            "type": 3,
            "event": "loader/add_plugin_install_prompt",
            "args": ["Other-Plugin", "1.0.0", "request-id", "a" * 64, 0],
        },
    ])

    with pytest.raises(RuntimeError, match="unexpected plugin"):
        target_plugin_install.install_and_confirm(
            ws, "http://127.0.0.1/plugin.zip", "0.5.1", "a" * 64, False
        )


def test_install_rejects_confirmation_with_a_different_hash() -> None:
    ws = FakeWebSocket([{
        "type": 3,
        "event": "loader/add_plugin_install_prompt",
        "args": ["CE Decky", "0.5.1", "request-id", "b" * 64, 0],
    }])

    with pytest.raises(RuntimeError, match="unexpected plugin"):
        target_plugin_install.install_and_confirm(
            ws, "file:///plugin.zip", "0.5.1", "a" * 64, False
        )


def test_decky_metadata_name_is_distinct_from_zip_root() -> None:
    assert target_plugin_install.PLUGIN_NAME == "CE Decky"
    assert target_plugin_install.PACKAGE_ROOT == "CE-Decky"


def _authority_report(
    plugin_root: Path, *, version: str, package_sha: str, backend: dict[str, object] | None = None,
) -> dict[str, object]:
    report: dict[str, object] = {
        "schema": 2,
        "ok": True,
        "installed": {"plugin_root": str(plugin_root), "checked_files": 302},
        "package_sha256": package_sha,
        "version": version,
    }
    if backend is not None:
        report["readback"] = {"backend": backend}
    return report


def _authority_tree(tmp_path: Path) -> tuple[Path, Path]:
    records = tmp_path / "target-installs"
    records.mkdir(parents=True)
    plugin_root = tmp_path / "homebrew" / "plugins" / "CE-Decky"
    plugin_root.mkdir(parents=True)
    return records, plugin_root


def _live_plugin_tree(tmp_path: Path, version: str = "0.9.15") -> tuple[Path, Path]:
    decky_home = tmp_path / "homebrew"
    plugin_root = decky_home / "plugins" / "CE-Decky"
    plugin_root.mkdir(parents=True)
    (plugin_root / "plugin.json").write_text(json.dumps({"name": "CE Decky"}), encoding="utf-8")
    (plugin_root / "package.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    settings_dir = decky_home / "settings" / "CE-Decky"
    settings_dir.mkdir(parents=True)
    return plugin_root, settings_dir


def _tree_identity(root: Path) -> dict[str, tuple[int, int, int]]:
    return {
        str(path.relative_to(root)): (
            path.lstat().st_mode,
            path.lstat().st_size,
            path.lstat().st_mtime_ns,
        )
        for path in root.rglob("*")
    }


def test_install_authority_selects_the_newest_successful_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    records, plugin_root = _authority_tree(tmp_path)
    old = records / "20260101T000000000000Z-aaaaaaaaaaaa.json"
    old.write_text(
        json.dumps(_authority_report(plugin_root, version="0.9.13", package_sha="a" * 64)),
        encoding="utf-8",
    )
    newest = records / "20260102T000000000000Z-bbbbbbbbbbbb.json"
    newest.write_text(
        json.dumps(_authority_report(plugin_root, version="0.9.14", package_sha="b" * 64)),
        encoding="utf-8",
    )
    # Anything in the directory that is not a successful install report is not
    # an authority, and must not become one by being the newest file there.
    (records / "20260103T000000000000Z-cccccccccccc.json").write_text(
        json.dumps({"schema": 1, "ok": True, "plugin_root": "/not/an/install/report"}),
        encoding="utf-8",
    )
    os.utime(old, ns=(1_000_000_000, 1_000_000_000))
    os.utime(newest, ns=(2_000_000_000, 2_000_000_000))
    before = _tree_identity(tmp_path)
    monkeypatch.setattr(
        target_plugin_install,
        "live_install_authority",
        lambda *_args: (_ for _ in ()).throw(target_plugin_install.LiveAuthorityUnavailable("Decky stopped")),
    )

    assert target_plugin_install.main([
        "authority", "--records-root", str(records),
    ]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result == {
        "bundle_sha256": None,
        "ok": True,
        "package_sha256": "b" * 64,
        "plugin_root": str(plugin_root),
        "record": str(newest),
        "schema": 1,
        "settings_dir": None,
        "source": "recorded_install",
        "user_home": None,
        "version": "0.9.14",
    }
    # Reading the authority is read-only.
    assert _tree_identity(tmp_path) == before


def test_the_recorded_authority_answers_with_the_decky_paths_it_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one moment those paths are wanted is when the backend cannot answer.

    A consumer reaching this has already failed to ask the live backend, and it
    is usually a probe about state on a device whose backend is the thing that
    stopped. The install validated both of these against that backend before
    writing them down, so reporting nothing sent the operator to find by hand
    what the record was holding.
    """
    records, plugin_root = _authority_tree(tmp_path)
    user_home = tmp_path / "home"
    user_home.mkdir()
    settings_dir = tmp_path / "homebrew" / "settings" / "CE-Decky"
    settings_dir.mkdir(parents=True)
    (records / "20260102T000000000000Z-bbbbbbbbbbbb.json").write_text(
        json.dumps(_authority_report(
            plugin_root, version="0.9.14", package_sha="b" * 64,
            backend={"user_home": str(user_home), "settings_dir": str(settings_dir)},
        )),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        target_plugin_install, "live_install_authority",
        lambda *_args: (_ for _ in ()).throw(target_plugin_install.LiveAuthorityUnavailable("Decky stopped")),
    )

    answer = target_plugin_install.resolve_install_authority(records)

    assert answer["source"] == "recorded_install"
    assert answer["user_home"] == str(user_home)
    assert answer["settings_dir"] == str(settings_dir)


def test_a_recorded_decky_path_that_is_gone_is_not_an_answer(tmp_path: Path, monkeypatch) -> None:
    """Recorded rather than live, so the filesystem is asked again.

    A home or a settings directory the record names and the device no longer
    has is not a path to hand a helper that is about to open it, and the shape
    check alone would pass it.
    """
    records, plugin_root = _authority_tree(tmp_path)
    (records / "20260102T000000000000Z-bbbbbbbbbbbb.json").write_text(
        json.dumps(_authority_report(
            plugin_root, version="0.9.14", package_sha="b" * 64,
            backend={
                "user_home": str(tmp_path / "gone"),
                "settings_dir": str(tmp_path / "homebrew" / "settings" / "CE-Decky"),
            },
        )),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        target_plugin_install, "live_install_authority",
        lambda *_args: (_ for _ in ()).throw(target_plugin_install.LiveAuthorityUnavailable("Decky stopped")),
    )

    answer = target_plugin_install.resolve_install_authority(records)

    assert answer["user_home"] is None and answer["settings_dir"] is None
    # A record from before the install wrote them down answers the same way.
    (records / "20260103T000000000000Z-cccccccccccc.json").write_text(
        json.dumps(_authority_report(plugin_root, version="0.9.14", package_sha="c" * 64)),
        encoding="utf-8",
    )
    assert target_plugin_install.resolve_install_authority(records)["user_home"] is None


def test_live_backend_self_report_is_the_primary_install_authority(tmp_path: Path) -> None:
    plugin_root, settings_dir = _live_plugin_tree(tmp_path)
    result = target_plugin_install._live_authority_from_answers(
        [{"name": "CE Decky", "version": "0.9.15", "disabled": False}],
        {
            "version": "0.9.15",
            "plugin_dir": str(plugin_root),
            "settings_dir": str(settings_dir),
        },
    )

    assert result == {
        "schema": 2,
        "ok": True,
        "source": "live_backend",
        "plugin_root": str(plugin_root),
        "version": "0.9.15",
        "bundle_sha256": None,
        "settings_dir": str(settings_dir),
        "user_home": None,
    }


def test_install_authority_carries_the_paths_the_other_probes_ask_for(tmp_path: Path) -> None:
    # The state and launch probes both want DECKY_USER_HOME and the plugin's
    # settings directory, and the environment probe that used to be the named
    # route for them can be denied the procfs bytes it reads. The live backend
    # reports both beside the root, in the same answer this command already
    # reads, so the pair costs no second call.
    plugin_root, settings_dir = _live_plugin_tree(tmp_path)
    home = tmp_path / "home" / "deck"
    home.mkdir(parents=True)

    result = target_plugin_install._live_authority_from_answers(
        [{"name": "CE Decky", "version": "0.9.15", "disabled": False}],
        {
            "version": "0.9.15",
            "plugin_dir": str(plugin_root),
            "settings_dir": str(settings_dir),
            "user_home": str(home),
        },
    )
    assert result["settings_dir"] == str(settings_dir)
    assert result["user_home"] == str(home)


def test_a_settings_path_outside_deckys_own_layout_is_reported_as_absent(tmp_path: Path) -> None:
    # A consumer fills a --settings-dir from this, so a value that is not one of
    # Decky's own settings directories is withheld rather than passed on. The
    # plugin root is the authority this command exists for and stays.
    plugin_root, _ = _live_plugin_tree(tmp_path)
    for bad in (str(tmp_path / "elsewhere" / "CE-Decky"), "relative/CE-Decky", str(tmp_path / "settings" / "Other"), ""):
        result = target_plugin_install._live_authority_from_answers(
            [{"name": "CE Decky", "version": "0.9.15", "disabled": False}],
            {"version": "0.9.15", "plugin_dir": str(plugin_root), "settings_dir": bad},
        )
        assert result["settings_dir"] is None, bad
        assert result["plugin_root"] == str(plugin_root)


def test_a_user_home_that_is_not_an_absolute_path_is_reported_as_absent(tmp_path: Path) -> None:
    plugin_root, settings_dir = _live_plugin_tree(tmp_path)
    for bad in ("home/deck", "/home/../etc", "", 7):
        result = target_plugin_install._live_authority_from_answers(
            [{"name": "CE Decky", "version": "0.9.15", "disabled": False}],
            {
                "version": "0.9.15",
                "plugin_dir": str(plugin_root),
                "settings_dir": str(settings_dir),
                "user_home": bad,
            },
        )
        assert result["user_home"] is None, bad

def test_install_authority_names_the_frontend_the_device_would_load(tmp_path: Path) -> None:
    # A development version is rebuilt and reinstalled many times without
    # moving, so the root and the version cannot say which of those builds is
    # on the device. The bundle can, and reading it here is what keeps that
    # answer to one command instead of a hand-made digest of Decky's own tree.
    plugin_root, settings_dir = _live_plugin_tree(tmp_path)
    (plugin_root / "dist").mkdir()
    (plugin_root / "dist" / "index.js").write_bytes(b"export const bundle = 1;\n")
    expected = hashlib.sha256(b"export const bundle = 1;\n").hexdigest()

    result = target_plugin_install._live_authority_from_answers(
        [{"name": "CE Decky", "version": "0.9.15", "disabled": False}],
        {"version": "0.9.15", "plugin_dir": str(plugin_root), "settings_dir": str(settings_dir)},
    )
    assert result["bundle_sha256"] == expected

    # An unreadable bundle says nothing rather than refusing the plugin root,
    # which is the authority this command exists to establish.
    (plugin_root / "dist" / "index.js").unlink()
    unreadable = target_plugin_install._live_authority_from_answers(
        [{"name": "CE Decky", "version": "0.9.15", "disabled": False}],
        {"version": "0.9.15", "plugin_dir": str(plugin_root), "settings_dir": str(settings_dir)},
    )
    assert unreadable["bundle_sha256"] is None
    assert unreadable["plugin_root"] == str(plugin_root)


def test_live_backend_bootstraps_an_installed_older_version_from_decky_layout(tmp_path: Path) -> None:
    plugin_root, settings_dir = _live_plugin_tree(tmp_path, version="0.9.14")
    result = target_plugin_install._live_authority_from_answers(
        [{"name": "CE Decky", "version": "0.9.14", "disabled": False}],
        {"version": "0.9.14", "settings_dir": str(settings_dir)},
    )

    assert result["source"] == "live_backend_legacy_layout"
    assert result["plugin_root"] == str(plugin_root)


def test_live_backend_refuses_a_reported_root_that_disagrees_with_installed_metadata(tmp_path: Path) -> None:
    plugin_root, settings_dir = _live_plugin_tree(tmp_path)
    (plugin_root / "package.json").write_text(json.dumps({"version": "different"}), encoding="utf-8")

    with pytest.raises(RuntimeError, match="metadata disagrees"):
        target_plugin_install._live_authority_from_answers(
            [{"name": "CE Decky", "version": "0.9.15", "disabled": False}],
            {"version": "0.9.15", "plugin_dir": str(plugin_root), "settings_dir": str(settings_dir)},
        )


def test_resolver_falls_back_to_a_durable_record_only_when_live_decky_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    records, plugin_root = _authority_tree(tmp_path)
    record = records / "20260101T000000000000Z-aaaaaaaaaaaa.json"
    record.write_text(
        json.dumps(_authority_report(plugin_root, version="0.9.15", package_sha="a" * 64)),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        target_plugin_install,
        "live_install_authority",
        lambda *_args: (_ for _ in ()).throw(target_plugin_install.LiveAuthorityUnavailable("Decky stopped")),
    )

    assert target_plugin_install.resolve_install_authority(records)["source"] == "recorded_install"


def test_resolver_does_not_hide_a_live_identity_conflict_with_an_old_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    records, plugin_root = _authority_tree(tmp_path)
    (records / "20260101T000000000000Z-aaaaaaaaaaaa.json").write_text(
        json.dumps(_authority_report(plugin_root, version="0.9.15", package_sha="a" * 64)),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        target_plugin_install,
        "live_install_authority",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("live identity conflict")),
    )

    with pytest.raises(RuntimeError, match="live identity conflict"):
        target_plugin_install.resolve_install_authority(records)


def test_default_install_records_live_outside_the_disposable_repository_build(tmp_path: Path) -> None:
    records = target_plugin_install.default_install_records({"XDG_STATE_HOME": str(tmp_path / "state")})
    assert records == tmp_path / "state" / "ce-decky-development" / "target-installs"
    assert "build" not in records.parts


def test_install_authority_ignores_a_record_whose_root_is_gone(tmp_path: Path) -> None:
    records, plugin_root = _authority_tree(tmp_path)
    (records / "20260101T000000000000Z-aaaaaaaaaaaa.json").write_text(
        json.dumps(_authority_report(tmp_path / "gone" / "plugins" / "CE-Decky",
                                     version="0.9.13", package_sha="a" * 64)),
        encoding="utf-8",
    )
    newest = records / "20260102T000000000000Z-bbbbbbbbbbbb.json"
    newest.write_text(
        json.dumps(_authority_report(plugin_root, version="0.9.14", package_sha="b" * 64)),
        encoding="utf-8",
    )
    # An installation that no longer exists is history, not a second live root.
    result = target_plugin_install.latest_install_authority(records)
    assert result["plugin_root"] == str(plugin_root)


def test_an_unwritable_record_directory_does_not_fail_a_finished_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    result = _authority_report(
        tmp_path / "homebrew" / "plugins" / "CE-Decky", version="0.9.15", package_sha="a" * 64
    )
    monkeypatch.setattr(target_plugin_install, "install_package", lambda *a, **k: result)

    def unwritable(*_args, **_kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(target_plugin_install, "record_install", unwritable)
    # The device is already mutated; a lost record must not read as a failure.
    assert target_plugin_install.main([
        "install", str(tmp_path / "package.zip"), "--json",
        "--sha256", "a" * 64, "--plugin-root", str(tmp_path / "root"),
    ]) == 0
    assert "install record not written" in capsys.readouterr().err


def test_install_command_resolves_the_root_when_the_caller_omits_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_root, _settings_dir = _live_plugin_tree(tmp_path)
    observed: list[Path] = []

    def install(_artifact, _sha, root, _url, _replace, _timeout, expect_version=None):
        observed.append((root, expect_version))
        return _authority_report(root, version="0.9.15", package_sha="a" * 64)

    monkeypatch.setattr(
        target_plugin_install,
        "resolve_install_authority",
        lambda *_args: {"plugin_root": str(plugin_root)},
    )
    monkeypatch.setattr(target_plugin_install, "install_package", install)
    monkeypatch.setattr(target_plugin_install, "record_install", lambda _result: None)

    assert target_plugin_install.main([
        "install", str(tmp_path / "package.zip"), "--json",
        "--sha256", "a" * 64, "--replace",
    ]) == 0
    # The version expectation is the repository's unless the caller names one,
    # which only the deliberately older update-path build ever does.
    assert observed == [(plugin_root, None)]


def test_install_records_are_bounded_and_readable_back(tmp_path: Path) -> None:
    records, plugin_root = _authority_tree(tmp_path)
    for index in range(target_plugin_install.MAX_RETAINED_INSTALL_RECORDS + 5):
        target_plugin_install.record_install(
            _authority_report(plugin_root, version="0.9.15", package_sha=f"{index:064d}"),
            records,
        )
    kept = sorted(records.glob("*.json"))
    assert len(kept) == target_plugin_install.MAX_RETAINED_INSTALL_RECORDS
    assert not list(records.glob("*.tmp"))
    # Ordinary use writes the record the read path expects.
    assert target_plugin_install.latest_install_authority(records)["plugin_root"] == str(plugin_root)


def test_install_authority_rejects_conflicting_live_roots(tmp_path: Path) -> None:
    records, plugin_root = _authority_tree(tmp_path)
    other_root = tmp_path / "other" / "plugins" / "CE-Decky"
    other_root.mkdir(parents=True)
    for index, root in enumerate((plugin_root, other_root), start=1):
        (records / f"2026010{index}T000000000000Z-{index * 12}.json").write_text(
            json.dumps(_authority_report(root, version="0.9.14", package_sha=f"{index}" * 64)),
            encoding="utf-8",
        )

    # Two installations that both still exist is an ambiguity, not a choice.
    with pytest.raises(RuntimeError, match="disagree on plugin_root"):
        target_plugin_install.latest_install_authority(records)


def test_install_authority_refuses_a_symlinked_record_directory(tmp_path: Path) -> None:
    records, plugin_root = _authority_tree(tmp_path)
    (records / "20260101T000000000000Z-aaaaaaaaaaaa.json").write_text(
        json.dumps(_authority_report(plugin_root, version="0.9.15", package_sha="a" * 64)),
        encoding="utf-8",
    )
    link = tmp_path / "records-link"
    link.symlink_to(records, target_is_directory=True)

    with pytest.raises(RuntimeError, match="missing or unsafe"):
        target_plugin_install.latest_install_authority(link)


def test_install_authority_refuses_a_symlinked_record(tmp_path: Path) -> None:
    records, plugin_root = _authority_tree(tmp_path)
    actual = tmp_path / "elsewhere.json"
    actual.write_text(
        json.dumps(_authority_report(plugin_root, version="0.9.15", package_sha="a" * 64)),
        encoding="utf-8",
    )
    (records / "20260101T000000000000Z-aaaaaaaaaaaa.json").symlink_to(actual)

    with pytest.raises(RuntimeError, match="symlink"):
        target_plugin_install.latest_install_authority(records)


def _artifact(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("CE-Decky/plugin.json", json.dumps({"name": "CE Decky"}))
        archive.writestr("CE-Decky/dist/index.js", "export default true;")
    return path


def test_installed_tree_readback_checks_every_packaged_file(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path / "plugin.zip")
    plugin = tmp_path / "homebrew" / "plugins" / "CE-Decky"
    (plugin / "dist").mkdir(parents=True)
    with zipfile.ZipFile(artifact) as archive:
        archive.extractall(plugin.parent)

    result = target_plugin_install.verify_installed_tree(artifact, plugin)

    assert result["checked_files"] == 2
    (plugin / "dist" / "index.js").write_text("changed", encoding="utf-8")
    with pytest.raises(RuntimeError, match="differs from the exact ZIP"):
        target_plugin_install.verify_installed_tree(artifact, plugin)


def test_installed_tree_readback_waits_for_deckys_async_replacement(monkeypatch, tmp_path: Path) -> None:
    artifact = tmp_path / "plugin.zip"
    plugin = tmp_path / "homebrew" / "plugins" / "CE-Decky"
    attempts = 0

    def verify(_artifact: Path, _plugin: Path) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("installed member still has the previous bytes")
        return {"checked_files": 302, "plugin_root": str(plugin)}

    monkeypatch.setattr(target_plugin_install, "verify_installed_tree", verify)
    monkeypatch.setattr(target_plugin_install.time, "sleep", lambda _seconds: None)

    waited = target_plugin_install._wait_for_installed_tree(artifact, plugin, 1.0)
    assert waited["checked_files"] == 302
    assert waited["attempts"] == 2
    assert attempts == 2


def test_webhelper_pid_probe_uses_exact_process_name(tmp_path: Path) -> None:
    for pid, name in ((10, "steamwebhelper"), (11, "not-steamwebhelper"), (12, "steamwebhelper")):
        process = tmp_path / str(pid)
        process.mkdir()
        (process / "comm").write_text(name + "\n", encoding="utf-8")

    assert target_plugin_install._steam_webhelper_pids(tmp_path) == {10, 12}


def _fake_proc(root: Path, processes: dict[int, str]) -> Path:
    for entry in root.iterdir():
        if entry.name.isdigit():
            (entry / "cmdline").unlink(missing_ok=True)
            entry.rmdir()
    for pid, argv in processes.items():
        process = root / str(pid)
        process.mkdir(exist_ok=True)
        # procfs hands back NUL separated argv with a trailing NUL, and
        # `setproctitle` leaves the whole title in the first of them.
        (process / "cmdline").write_bytes(argv.encode("utf-8") + b"\x00")
    return root


def test_the_backend_probe_matches_the_exact_installed_root(tmp_path: Path) -> None:
    """A plugin process is named after the exact main.py Decky started it from."""
    root = tmp_path / "proc"
    root.mkdir()
    _fake_proc(root, {
        11: "CE Decky (/home/deck/homebrew/plugins/CE-Decky/main.py)",
        12: "CE Decky (/somewhere/else/CE-Decky/main.py)",
        13: "CE Decky",
        14: "python3 /home/deck/homebrew/plugins/CE-Decky/main.py",
    })

    assert target_plugin_install._plugin_backend_pids(
        Path("/home/deck/homebrew/plugins/CE-Decky"), root,
    ) == {11}


def _settle_over(monkeypatch, generations: list[set[int]], *, confirmed: bool) -> dict:
    """Run the settle wait over a scripted sequence of one-second observations."""
    clock = [0.0]
    observed: list[tuple[float, frozenset[int]]] = []

    def tick(_seconds: float) -> None:
        clock[0] += 1.0

    def pids(_root, proc_root=None) -> set[int]:
        current = generations.pop(0) if generations else observed[-1][1]
        observed.append((clock[0], frozenset(current)))
        return set(current)

    monkeypatch.setattr(target_plugin_install.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(target_plugin_install.time, "sleep", tick)
    monkeypatch.setattr(target_plugin_install, "_plugin_backend_pids", pids)
    settled = target_plugin_install._wait_for_settled_backend(
        Path("/home/deck/homebrew/plugins/CE-Decky"), confirmed=confirmed, timeout=60.0,
    )
    return {**settled, "observations": observed}


def test_an_unconfirmed_install_waits_out_a_generation_decky_is_about_to_replace(monkeypatch) -> None:
    """The watcher's load can be the only process for longer than it lives.

    Decky's installer disables its hot reload watcher and the uninstall it
    performs first switches it back on, so the watcher loads the plugin from the
    tree being written and the installer then stops that load and starts its
    own. The stop is SIGTERM, five seconds, then SIGKILL, so the doomed process
    is the single observable one for that whole time: the two replacements
    measured on this device took seven and six seconds from one `Loaded CE Decky`
    to the next. Sampling four seconds of it and calling it settled is how the
    reload went out between the two loads, which is the duplicate this wait
    exists to prevent.

    So without the install's own reply there is no generation boundary to stand
    on, and the window has to be longer than that whole life.
    """
    # Seven seconds of the doomed generation, a second with nothing running,
    # then its replacement for as long as it is asked about.
    generations = [{101} for _ in range(7)] + [set()] + [{102} for _ in range(20)]

    settled = _settle_over(monkeypatch, generations, confirmed=False)

    assert settled["settled"] is True
    # Never the one the watcher started, however long it was the only one.
    assert settled["pid"] == 102
    assert settled["settle_s"] == target_plugin_install.PLUGIN_BACKEND_REPLACEMENT_SECONDS
    # And the reload was not permitted until after the replacement had held for
    # that window: the first observation of 102 plus the window, at least.
    first_102 = next(at for at, pids in settled["observations"] if pids == frozenset({102}))
    assert settled["waited_s"] >= first_102 + target_plugin_install.PLUGIN_BACKEND_REPLACEMENT_SECONDS


def test_a_confirmed_install_needs_only_the_process_it_already_proved(monkeypatch) -> None:
    """Decky's reply is the boundary, so the window is about the process alone.

    `confirm_plugin_install` is awaited across the whole of `_install`, so its
    reply arrives after the final `import_plugin` has returned and no further
    load can start: the watcher's remaining queue entries ask for a refresh of a
    plugin that is loaded, which Decky refuses. Waiting out another replacement
    window there would spend ten seconds proving something already proven.
    """
    settled = _settle_over(monkeypatch, [{102} for _ in range(10)], confirmed=True)

    assert settled["settled"] is True and settled["pid"] == 102
    assert settled["settle_s"] == target_plugin_install.PLUGIN_BACKEND_SETTLE_SECONDS
    assert settled["waited_s"] <= target_plugin_install.PLUGIN_BACKEND_SETTLE_SECONDS + 1


def test_a_backend_that_never_settles_is_reported_rather_than_failing(tmp_path: Path, monkeypatch) -> None:
    """The files and the readback are already proven; this is the panel's shape.

    A host whose procfs answers differently, or a Decky that is still replacing
    the plugin when the bound runs out, must not fail an install that worked.
    Both say so on stderr and in the report, because both leave a duplicate
    panel entry possible until the next reload.
    """
    clock = [0.0]

    def tick(_seconds: float) -> None:
        clock[0] += 1.0

    monkeypatch.setattr(target_plugin_install.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(target_plugin_install.time, "sleep", tick)
    monkeypatch.setattr(target_plugin_install, "_plugin_backend_pids", lambda *_args, **_kwargs: set())

    settled = target_plugin_install._wait_for_settled_backend(
        Path("/home/deck/homebrew/plugins/CE-Decky"), confirmed=True, timeout=5.0,
    )

    assert settled["settled"] is False
    assert settled["observed_pids"] == []


def _panel_journal(state_root: Path, entries: list[dict]) -> Path:
    """Replace the record the way its own trim does, which is atomically.

    Through the production write rather than `write_text`, because that is the
    difference this is used to model: a rewrite in place keeps the file, and a
    trim replaces it, so only one of the two invalidates a boundary taken into
    the old one.
    """
    state_root.mkdir(parents=True, exist_ok=True)
    path = state_root / "frontend-journal.jsonl"
    atomic_write_bytes(
        path,
        "".join(json.dumps(entry) + "\n" for entry in entries).encode("utf-8"),
        mode=frontend_journal.JOURNAL_MODE,
    )
    return path


def _panel_append(state_root: Path, entries: list[dict]) -> None:
    """Add to the record the way the backend does, without replacing it."""
    state_root.mkdir(parents=True, exist_ok=True)
    with (state_root / "frontend-journal.jsonl").open("a", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")


def _boundary(state_root: Path) -> dict:
    """What the install takes when it asks for its reload."""
    return target_plugin_install._panel_boundary(state_root, RELOAD_AT)


# The reload every case below is about, and the two renderers either side of
# it. The new one is 1.3 seconds after the request, which is what this device
# measured rather than a round number: Steam starts the replacement renderer at
# once, and Decky's frontend load, which is the five seconds one might mistake
# for it, happens later inside that renderer. The outgoing one is five seconds
# before the request, which is the tightest shape there is: a first install
# shortly after Steam started.
RELOAD_AT = 1_700_000_000.0
NEW_RENDERER_STARTED_MS = str(int((RELOAD_AT + 1.3) * 1000))
OLD_RENDERER_STARTED_MS = str(int((RELOAD_AT - 5.0) * 1000))


def _mount(
    at: str, instance: str = "abc-1", session: str = "abc", renderer: str = "r-new",
    started_ms: str | None = NEW_RENDERER_STARTED_MS,
) -> dict:
    fields = {"panel_instance": instance, "panel_renderer": renderer}
    if started_ms is not None:
        fields["renderer_started_at_ms"] = started_ms
    return {"at": at, "level": "info", "event": "panel.mounted", "fields": fields, "session": session}


def _dismount(
    at: str, instance: str, session: str = "abc", renderer: str = "r-new",
    started_ms: str | None = NEW_RENDERER_STARTED_MS,
) -> dict:
    fields = {"panel_instance": instance, "panel_renderer": renderer}
    if started_ms is not None:
        fields["renderer_started_at_ms"] = started_ms
    return {"at": at, "level": "info", "event": "panel.dismounted", "fields": fields, "session": session}


def _summary_line(panel: dict) -> str:
    """The one line an install prints, for a report carrying this panel state."""
    return target_plugin_install._summary({
        "version": "0.9.24", "package_sha256": "a" * 64, "installed": {"checked_files": 307},
        "running_apps": {
            "observed": True,
            "before": {"available": True, "app_ids": []},
            "after": {"available": True, "app_ids": []},
            "stopped": [],
        },
        "panel_import": panel, "frontend_reload": {"observed": True, "before_count": 9, "after_count": 7},
    })


def test_the_panel_record_counts_only_what_was_appended_after_the_boundary(tmp_path: Path) -> None:
    """The boundary is where the file ended, not a moment on anybody's clock."""
    state = tmp_path / "state"
    _panel_journal(state, [_mount("2026-09-13T15:00:00.000Z", "old-1", session="old", renderer="r-old")])
    boundary = _boundary(state)
    _panel_append(state, [
        {"at": "2026-09-13T15:02:55.000Z", "level": "info", "event": "panel.visibility_changed", "fields": {}},
        _mount("2026-09-13T15:02:55.024Z", "new-1", session="new"),
    ])

    assert target_plugin_install._panel_instances_after(state, boundary) == {
        "imports": 1, "live": 1, "unpairable": 0, "readable": True,
    }
    # And from the beginning of the file, both of them.
    assert target_plugin_install._panel_instances_after(state, _boundary(tmp_path / "absent"))["imports"] in (0, 2)


def test_a_record_stamped_in_the_clocks_future_is_not_this_reloads_evidence(tmp_path: Path, monkeypatch) -> None:
    """This record outlives the renderer, and the moments in it are its clock.

    A clock corrected backwards leaves the previous frontend's entries stamped
    later than the clock this helper is holding. Filtering on that took them for
    this reload's: one stale unmatched mount certifies the quiet case and two
    report a duplicate, while the new frontend may not have imported anything
    yet. The helper already treats a persisted moment in its own future as a
    clock that moved rather than as a fact, and the boundary here is a place in
    a file, which no clock can move.
    """
    state = tmp_path / "state"
    ahead = datetime.fromtimestamp(time.time() + 3600, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    _panel_journal(state, [_mount(ahead, "stale-1", session="stale", renderer="r-stale")])
    boundary = _boundary(state)
    monkeypatch.setattr(target_plugin_install.time, "sleep", lambda _seconds: None)

    # Nothing has been appended since, so this reload has no evidence at all.
    panel = target_plugin_install._wait_for_panel_import(state, boundary, timeout=1.0)
    assert panel["cardinality"] == "unobserved"
    assert panel["observed"] is False

    # And the new frontend's own record is recognised even though the rollback
    # makes its moment earlier than the stale one above.
    behind = datetime.fromtimestamp(time.time() - 60, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    _panel_append(state, [_mount(behind, "fresh-1", session="fresh")])

    panel = target_plugin_install._wait_for_panel_import(state, boundary, timeout=5.0)
    assert panel["cardinality"] == "one" and panel["live_instances"] == 1


def test_a_record_replaced_under_the_boundary_is_unverified_rather_than_read(tmp_path: Path, monkeypatch) -> None:
    """A trim rewrites this file, and an offset into the old one means nothing."""
    state = tmp_path / "state"
    _panel_journal(state, [_mount("2026-09-13T15:00:00.000Z", "old-1", session="old")])
    boundary = _boundary(state)
    monkeypatch.setattr(target_plugin_install.time, "sleep", lambda _seconds: None)
    # The trim keeps the tail and replaces the file, so what is there now is
    # neither the same file nor longer than the boundary.
    _panel_journal(state, [_mount("2026-09-13T15:02:55.024Z", "new-1", session="new")])

    panel = target_plugin_install._wait_for_panel_import(state, boundary, timeout=1.0)

    assert panel["cardinality"] == "unverified"
    assert "replaced" in str(panel["reason"])
    assert "panel cardinality unverified" in _summary_line(panel) or "unobserved" in _summary_line(panel)


def test_a_late_flush_from_the_frontend_being_replaced_is_not_this_reloads_panel(tmp_path: Path, monkeypatch) -> None:
    """The outgoing frontend is alive when the boundary is taken, and writes late.

    Its entries reach the file on a five second timer, so a mount it recorded
    before the boundary can land after it, from a renderer the record has never
    seen: Decky does not reliably ask that frontend to import a newly installed
    plugin, and on this device's 19:39 install it recorded nothing at all, so
    there is nothing to wait for and nothing to recognise it by.

    What places it is its own age. A renderer this reload created cannot be
    older than the reload; the one being replaced is older than it by however
    long it had been running.
    """
    state = tmp_path / "state"
    state.mkdir(parents=True)
    boundary = _boundary(state)
    monkeypatch.setattr(target_plugin_install.time, "sleep", lambda _seconds: None)

    # Ten minutes old at the moment it imported, against a reload twenty
    # seconds ago: it was there before this install.
    _panel_append(state, [
        _mount("2026-09-13T15:00:20.000Z", "late-1", session="late", renderer="r-old", started_ms=OLD_RENDERER_STARTED_MS),
    ])
    panel = target_plugin_install._wait_for_panel_import(state, boundary, timeout=1.0)
    assert panel["cardinality"] == "unobserved"

    # The same, with the outgoing frontend also recording that it went.
    _panel_append(state, [
        _dismount("2026-09-13T15:00:21.000Z", "late-1", session="late", renderer="r-old", started_ms=OLD_RENDERER_STARTED_MS),
    ])
    panel = target_plugin_install._wait_for_panel_import(state, boundary, timeout=1.0)
    assert panel["cardinality"] == "unobserved"

    # And then the frontend this reload actually produced, three seconds old.
    _panel_append(state, [_mount("2026-09-13T15:00:25.000Z", "fresh-1", session="fresh", renderer="r-fresh")])
    panel = target_plugin_install._wait_for_panel_import(state, boundary, timeout=5.0)
    assert panel["cardinality"] == "one" and panel["live_instances"] == 1
    assert panel["imports"] == 1


def test_a_row_reads_the_same_way_however_long_the_helper_has_been_waiting(tmp_path: Path, monkeypatch) -> None:
    """The record is fixed, so what it says about itself must be fixed too.

    An age recorded once and compared against how long the reader has been
    waiting moves: a renderer that started five seconds before the reload and
    imported a second later carries six seconds, which reads as outgoing at
    three seconds into the wait and as new at five. The row from the frontend
    being destroyed would then become the row that replaced it, and the install
    would call that the quiet case. Two fixed moments cannot do that.
    """
    state = tmp_path / "state"
    state.mkdir(parents=True)
    boundary = _boundary(state)
    clock = [1000.0]
    monkeypatch.setattr(target_plugin_install.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        target_plugin_install.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )
    # The frontend being replaced: created five seconds before the reload was
    # asked for, which is what a first install shortly after Steam starts finds.
    _panel_append(state, [
        _mount("2026-09-13T15:00:01.000Z", "out-1", session="out", renderer="r-out",
               started_ms=OLD_RENDERER_STARTED_MS),
    ])

    for waited in (0.0, 3.0, 5.0, 10.0, 60.0):
        clock[0] = 1000.0 + waited
        counted = target_plugin_install._panel_instances_after(state, boundary)
        assert counted["imports"] == 0, f"the outgoing row was counted {waited}s into the wait"

    # Including through the wait itself, whose settled read is a second pass
    # over the same file.
    clock[0] = 1000.0
    assert target_plugin_install._wait_for_panel_import(state, boundary, timeout=6.0)["cardinality"] == "unobserved"

    # And the one this reload created stays new on every pass.
    _panel_append(state, [_mount("2026-09-13T15:00:06.000Z", "in-1", session="in", renderer="r-in")])
    for waited in (0.0, 3.0, 60.0):
        clock[0] = 1000.0 + waited
        counted = target_plugin_install._panel_instances_after(state, boundary)
        assert counted["imports"] == 1 and counted["live"] == 1
    assert target_plugin_install._wait_for_panel_import(state, boundary, timeout=6.0)["cardinality"] == "one"


def test_a_reinstall_after_a_removal_does_not_read_the_record_it_left(tmp_path: Path, monkeypatch) -> None:
    """Removing this plugin does not remove this plugin's state.

    `_uninstall()` is deliberately non-destructive and the panel's record lives
    under the user's home, so a reinstall after an ordinary removal finds a
    record full of rows from renderers that are long gone. None of them is this
    reload's: they are behind its position, and the frontend that is alive now
    is older than the reload whether or not the record has ever seen it.
    """
    state = tmp_path / "state"
    _panel_journal(state, [
        _mount("2026-09-13T14:00:00.000Z", "gone-1", session="gone", renderer="r-gone", started_ms=OLD_RENDERER_STARTED_MS),
    ])
    boundary = _boundary(state)
    monkeypatch.setattr(target_plugin_install.time, "sleep", lambda _seconds: None)

    # The frontend actually being replaced, which this record has never seen,
    # flushing after the boundary.
    _panel_append(state, [
        _mount("2026-09-13T15:00:20.000Z", "cur-1", session="cur", renderer="r-current", started_ms=OLD_RENDERER_STARTED_MS),
    ])

    panel = target_plugin_install._wait_for_panel_import(state, boundary, timeout=1.0)
    assert panel["cardinality"] == "unobserved"

    _panel_append(state, [_mount("2026-09-13T15:00:25.000Z", "fresh-1", session="fresh", renderer="r-fresh")])
    panel = target_plugin_install._wait_for_panel_import(state, boundary, timeout=5.0)
    assert panel["cardinality"] == "one" and panel["live_instances"] == 1


def test_a_record_that_places_itself_nowhere_cannot_be_counted(tmp_path: Path, monkeypatch) -> None:
    """A build older than these fields leaves the count unverified, never quiet."""
    state = tmp_path / "state"
    state.mkdir(parents=True)
    boundary = _boundary(state)
    monkeypatch.setattr(target_plugin_install.time, "sleep", lambda _seconds: None)
    entry = {
        "at": "2026-09-13T15:02:55.024Z", "level": "info", "event": "panel.mounted",
        "fields": {"panel_instance": "old-build-1"}, "session": "abc",
    }
    _panel_append(state, [entry])

    panel = target_plugin_install._wait_for_panel_import(state, boundary, timeout=5.0)

    assert panel["imports"] == 1 and panel["unpairable_imports"] == 1
    assert panel["live_instances"] == 0
    assert panel["cardinality"] == "unverified"


def test_an_import_that_replaced_the_row_before_it_is_not_a_duplicate(tmp_path: Path, monkeypatch) -> None:
    """Two imports in sequence are two mounts, one dismount and one row.

    Decky's own `importPlugin` removes the plugin's existing row before it
    appends the replacement, and removing it calls the plugin's teardown, which
    is what records the dismount. Counting imports alone would call this a
    duplicated panel and send the operator to replace Steam's webhelper for
    nothing, inside the window where three of those stop Decky's service.
    """
    state = tmp_path / "state"
    boundary = _boundary(state)
    _panel_append(state, [
        _mount("2026-09-13T15:02:55.024Z", "first-1", session="first"),
        _dismount("2026-09-13T15:03:10.000Z", "first-1", session="first"),
        _mount("2026-09-13T15:03:10.100Z", "second-1", session="second"),
    ])
    monkeypatch.setattr(target_plugin_install.time, "sleep", lambda _seconds: None)

    panel = target_plugin_install._wait_for_panel_import(state, boundary, timeout=5.0)

    assert panel["imports"] == 2
    assert panel["live_instances"] == 1
    assert panel["cardinality"] == "one"
    assert "panel" not in _summary_line(panel)


def test_two_lifetimes_that_were_never_dismounted_are_the_duplicated_panel(tmp_path: Path, monkeypatch) -> None:
    """The race leaves both imports holding a row, and neither dismounts.

    One install loads the plugin twice and the panel's bulk import on startup
    takes no lock, so an import event landing in the middle of it runs beside
    it: both find no row of their own to remove, both append, and the panel
    lists the plugin twice over one backend. That is the one history where the
    mounts stand unmatched, which is what this tells apart.
    """
    state = tmp_path / "state"
    boundary = _boundary(state)
    _panel_append(state, [
        _mount("2026-09-13T15:02:55.024Z", "first-1", session="first"),
        _mount("2026-09-13T15:02:55.090Z", "second-1", session="second"),
    ])
    monkeypatch.setattr(target_plugin_install.time, "sleep", lambda _seconds: None)

    panel = target_plugin_install._wait_for_panel_import(state, boundary, timeout=5.0)

    assert panel["imports"] == 2 and panel["live_instances"] == 2
    assert panel["cardinality"] == "duplicate"
    assert "panel lists it 2 times over one backend" in _summary_line(panel)


def test_two_panels_from_one_loaded_module_are_still_two_panels(tmp_path: Path, monkeypatch) -> None:
    """One module can be asked for two rows, and both write the same batch id.

    Decky imports the bundle as `index.js?t=${Date.now()}` and a browser returns
    one module instance per resolved URL, so two imports issued inside one
    millisecond evaluate the module once and call its factory twice. The
    entries' `session` names that module, so pairing rows by it would collapse
    two into one and report the quiet case for exactly the concurrency this
    check exists to catch. Worse, dismounting either of them would then discard
    the only key and take the other's evidence with it.
    """
    state = tmp_path / "state"
    boundary = _boundary(state)
    monkeypatch.setattr(target_plugin_install.time, "sleep", lambda _seconds: None)
    # One session, two factory invocations.
    _panel_append(state, [
        _mount("2026-09-13T15:02:55.024Z", "shared-1", session="shared"),
        _mount("2026-09-13T15:02:55.090Z", "shared-2", session="shared"),
    ])

    panel = target_plugin_install._wait_for_panel_import(state, boundary, timeout=5.0)
    assert panel["imports"] == 2 and panel["live_instances"] == 2
    assert panel["cardinality"] == "duplicate"

    # The first row is dropped, and the second is still there.
    _panel_append(state, [_dismount("2026-09-13T15:04:00.000Z", "shared-1", session="shared")])
    panel = target_plugin_install._wait_for_panel_import(state, boundary, timeout=5.0)
    assert panel["live_instances"] == 1 and panel["cardinality"] == "one"

    # And then the second.
    _panel_append(state, [_dismount("2026-09-13T15:04:01.000Z", "shared-2", session="shared")])
    panel = target_plugin_install._wait_for_panel_import(state, boundary, timeout=5.0)
    assert panel["live_instances"] == 0 and panel["cardinality"] == "missing"


def test_one_panel_import_is_the_quiet_case(tmp_path: Path, monkeypatch) -> None:
    state = tmp_path / "state"
    boundary = _boundary(state)
    _panel_append(state, [_mount("2026-09-13T15:02:55.024Z")])
    monkeypatch.setattr(target_plugin_install.time, "sleep", lambda _seconds: None)

    panel = target_plugin_install._wait_for_panel_import(state, boundary, timeout=5.0)

    assert panel["imports"] == 1 and panel["live_instances"] == 1
    assert panel["cardinality"] == "one"
    assert "panel" not in _summary_line(panel)


def test_a_panel_that_imported_and_kept_nothing_is_not_the_quiet_case(tmp_path: Path, monkeypatch) -> None:
    """No row is a different answer from one row, and it must not read as one.

    The plugin is installed and its backend is running; what is missing is the
    row. An install that says nothing about it is read as one that put a row
    there, which is the one thing this check exists to say.
    """
    state = tmp_path / "state"
    boundary = _boundary(state)
    _panel_append(state, [
        _mount("2026-09-13T15:02:55.024Z", "only-1", session="only"),
        _dismount("2026-09-13T15:03:00.000Z", "only-1", session="only"),
    ])
    monkeypatch.setattr(target_plugin_install.time, "sleep", lambda _seconds: None)

    panel = target_plugin_install._wait_for_panel_import(state, boundary, timeout=5.0)

    assert panel["imports"] == 1 and panel["live_instances"] == 0
    assert panel["cardinality"] == "missing"
    assert "panel holds no CE Decky row" in _summary_line(panel)


def test_a_mount_with_no_lifetime_id_leaves_the_count_unverified(tmp_path: Path, monkeypatch) -> None:
    """What a wrong yes costs here is a webhelper replacement nobody needed.

    An unpairable mount is not evidence of a second row, and it is not evidence
    of one row either: it may itself be another. Beside a row that is known, it
    still leaves the count unknowable, so the answer is that rather than either
    of the two it could have been.
    """
    state = tmp_path / "state"
    boundary = _boundary(state)
    # What a record written by a build older than the per-row id looks like.
    entry = {
        "at": "2026-09-13T15:02:55.024Z", "level": "info", "event": "panel.mounted",
        "fields": {}, "session": "abc",
    }
    _panel_append(state, [entry, {**entry, "at": "2026-09-13T15:02:55.090Z"}])
    monkeypatch.setattr(target_plugin_install.time, "sleep", lambda _seconds: None)

    panel = target_plugin_install._wait_for_panel_import(state, boundary, timeout=5.0)

    assert panel["imports"] == 2
    assert panel["unpairable_imports"] == 2
    assert panel["live_instances"] == 0
    assert panel["cardinality"] == "unverified"
    assert "panel cardinality unverified, 2 import(s) that belong to no known generation" in _summary_line(panel)

    # And beside one row that is known, which is the case that used to read as
    # the quiet one.
    _panel_append(state, [_mount("2026-09-13T15:02:56.000Z", "known-1", session="known")])
    panel = target_plugin_install._wait_for_panel_import(state, boundary, timeout=5.0)
    assert panel["live_instances"] == 1 and panel["cardinality"] == "unverified"


def test_a_panel_that_records_nothing_is_unobserved_rather_than_a_failure(tmp_path: Path, monkeypatch) -> None:
    """The files, the inventory and the backend are already proven by here.

    A panel record that never arrives says this install could not be checked,
    which is not the same as an install that did not work, and it must not fail
    one. It is on the summary line either way.
    """
    clock = [0.0]
    monkeypatch.setattr(target_plugin_install.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(target_plugin_install.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    state = tmp_path / "state"

    panel = target_plugin_install._wait_for_panel_import(state, _boundary(state), timeout=3.0)

    assert panel["observed"] is False and panel["imports"] == 0
    assert panel["cardinality"] == "unobserved"
    assert "recorded no import" in str(panel["reason"])
    assert "panel import unobserved" in _summary_line(panel)
    # And a backend that reports no usable home leaves it unread rather than guessed.
    assert target_plugin_install._panel_state_root({"backend": {"user_home": "  "}}) is None
    assert target_plugin_install._panel_state_root({"backend": {"user_home": "/home/deck"}}) == Path(
        "/home/deck/.cheat-engine-decky/state",
    )


def test_package_hash_helper_is_binary_exact(tmp_path: Path) -> None:
    payload = b"CE Decky\x00\xff"
    path = tmp_path / "payload"
    path.write_bytes(payload)
    assert target_plugin_install._sha256_file(path) == sha256(payload).hexdigest()


def test_loader_inventory_filters_only_exact_metadata_name() -> None:
    ws = FakeWebSocket([{
        "type": 1,
        "id": 10,
        "result": [
            {"name": "CE-Decky", "version": "wrong-directory-name"},
            {"name": "CE Decky", "version": "0.5.1"},
        ],
    }])

    assert target_plugin_install._loader_plugin_matches(ws, 10) == [
        {"name": "CE Decky", "version": "0.5.1"}
    ]


def test_frontend_reload_accepts_an_explicit_reply() -> None:
    ws = FakeWebSocket([{"type": 1, "id": 5, "result": None}])

    assert target_plugin_install._request_frontend_reload(ws) is True
    assert ws.sent == [{
        "type": 0,
        "route": "utilities/restart_webhelper",
        "args": [],
        "id": 5,
    }]


def test_install_readback_confirms_the_backend_uses_the_exact_installed_root(tmp_path: Path) -> None:
    plugin_root = tmp_path / "homebrew" / "plugins" / "CE-Decky"
    ws = FakeWebSocket([
        {"type": 1, "id": 3, "result": [
            {"name": "CE Decky", "version": "0.9.15", "disabled": False},
        ]},
        {"type": 1, "id": 4, "result": {
            "version": "0.9.15", "plugin_dir": str(plugin_root),
            "user_home": str(tmp_path), "settings_dir": str(tmp_path / "settings"),
        }},
    ])

    result = target_plugin_install._validate_loader_readback(ws, "0.9.15", plugin_root)
    assert result["backend"]["plugin_dir"] == str(plugin_root)

    wrong = FakeWebSocket([
        {"type": 1, "id": 3, "result": [
            {"name": "CE Decky", "version": "0.9.15", "disabled": False},
        ]},
        {"type": 1, "id": 4, "result": {
            "version": "0.9.15", "plugin_dir": str(tmp_path / "other" / "plugins" / "CE-Decky"),
        }},
    ])
    with pytest.raises(RuntimeError, match="different installed plugin root"):
        target_plugin_install._validate_loader_readback(wrong, "0.9.15", plugin_root)


def test_frontend_reload_defers_a_closed_socket_to_process_replacement_proof() -> None:
    class ClosingWebSocket(FakeWebSocket):
        def receive_json(self) -> dict[str, object]:
            raise target_plugin_install.DeckyWebSocketClosed("expected self-termination")

    ws = ClosingWebSocket([])

    assert target_plugin_install._request_frontend_reload(ws) is False
    assert ws.sent[0]["route"] == "utilities/restart_webhelper"


def test_frontend_reload_does_not_hide_an_explicit_rpc_failure() -> None:
    ws = FakeWebSocket([{"type": -1, "id": 5, "error": {"message": "reload denied"}}])

    with pytest.raises(RuntimeError, match="reload denied"):
        target_plugin_install._request_frontend_reload(ws)


def test_the_default_output_is_one_line_a_reader_can_take_in() -> None:
    # The full report is mostly readback that only matters when something went
    # wrong, and this runs several times a session. One line every time; the
    # rest is one flag away.
    summary = target_plugin_install._summary({
        "version": "0.9.5",
        "package_sha256": "a" * 64,
        "installed": {"checked_files": 294},
        "frontend_reload": {"observed": True, "before_count": 9, "after_count": 7},
        "running_apps": target_plugin_install._running_app_report(_apps([]), _apps([])),
    })
    assert "\n" not in summary
    assert "CE Decky 0.9.5 installed" in summary
    assert "294 files verified" in summary
    assert "webhelper 9\u21927" in summary
    assert "no Steam app was running" in summary


def test_the_summary_still_names_a_game_the_install_landed_over() -> None:
    summary = target_plugin_install._summary({
        "version": "0.9.5",
        "package_sha256": "a" * 64,
        "installed": {"checked_files": 294},
        "frontend_reload": {"observed": True, "before_count": 9, "after_count": 7},
        "running_apps": target_plugin_install._running_app_report(_apps([10]), _apps([])),
    })
    assert "AppID 10 was running" in summary
    assert "no longer running" in summary


def _apps(app_ids: list[int], *, available: bool = True, reason: str | None = None) -> dict[str, object]:
    return {"available": available, "app_ids": app_ids, "scanned": 12, "reason": reason}


def test_install_reports_whether_a_game_was_running_and_what_stopped() -> None:
    # Installing reloads Decky's frontend, which replaces Steam's webhelper and
    # can take a playing game's controller input with it. Whether that happened
    # over a running game is part of what the install did.
    report = target_plugin_install._running_app_report(_apps([10, 20]), _apps([20]))
    assert report["observed"] is True
    assert report["any_running"] is True
    assert report["stopped"] == [10]
    assert "AppID 10, AppID 20 were running" in target_plugin_install._describe_running_apps(report)
    assert "AppID 10 is no longer running" in target_plugin_install._describe_running_apps(report)

    quiet = target_plugin_install._running_app_report(_apps([]), _apps([]))
    assert quiet["any_running"] is False
    assert quiet["stopped"] == []
    assert target_plugin_install._describe_running_apps(quiet) == "no Steam app was running"


def test_install_never_reads_a_truncated_scan_as_a_game_it_closed() -> None:
    # A bounded scan that could not finish must not turn "this install could not
    # see the whole process table" into "this install closed your game".
    truncated = target_plugin_install._running_app_report(
        _apps([], available=False, reason="process table exceeds the bounded scan limit"),
        _apps([10]),
    )
    assert truncated["observed"] is False
    assert truncated["any_running"] is None
    assert truncated["stopped"] is None
    assert "could not be observed" in target_plugin_install._describe_running_apps(truncated)


def test_a_version_disagreement_names_both_sides_and_the_ordinary_reason():
    """The refusal has to be readable, because it looks like the alarming case.

    Immediately after an install the tree on disk carries the new version while
    Decky's inventory still reports the old one, which produces the same refusal
    as a machine holding a plugin this run must not authorize. The message named
    neither version, so it pointed at the second explanation first.
    """
    message = target_plugin_install._disagreement(
        "live plugin metadata disagrees with Decky inventory",
        "the installed tree", "0.9.17", "0.9.16",
    )

    assert "'0.9.17'" in message and "'0.9.16'" in message
    assert "the installed tree" in message
    assert "read the authority again" in message
    assert "real identity conflict" in message


def test_a_disagreement_is_still_refused_rather_than_waited_out(monkeypatch):
    """Waiting quietly for the numbers to agree is how a real conflict hides."""
    matches = [{"name": target_plugin_install.PLUGIN_NAME, "version": "0.9.16", "disabled": False}]
    status = {"version": "0.9.17"}

    with pytest.raises(RuntimeError, match="backend identity disagrees"):
        target_plugin_install._live_authority_from_answers(matches, status)


def test_a_backend_that_answered_with_nothing_is_not_reported_as_a_disagreement():
    matches = [{"name": target_plugin_install.PLUGIN_NAME, "version": "0.9.17", "disabled": False}]

    with pytest.raises(RuntimeError, match="no status"):
        target_plugin_install._live_authority_from_answers(matches, None)


@pytest.mark.parametrize("status", [{}, {"version": None}, {"version": ""}, {"version": 917}])
def test_missing_or_malformed_backend_version_is_not_a_disagreement(status):
    """Malformed identity data is not two valid versions disagreeing.

    A loader catching up cannot be inferred from a version that is absent, null
    or not a string, so pointing the reader at that explanation would send them
    to wait for something that will never resolve.
    """
    matches = [{"name": target_plugin_install.PLUGIN_NAME, "version": "0.9.17", "disabled": False}]

    with pytest.raises(RuntimeError, match="no usable version") as refusal:
        target_plugin_install._live_authority_from_answers(matches, status)

    assert "read the authority again" not in str(refusal.value)


def test_an_installed_package_without_a_usable_version_says_that(tmp_path, monkeypatch):
    matches = [{"name": target_plugin_install.PLUGIN_NAME, "version": "0.9.17", "disabled": False}]
    status = {"version": "0.9.17", "plugin_dir": str(tmp_path)}

    monkeypatch.setattr(target_plugin_install, "_is_installed_plugin_root", lambda _root: True)
    monkeypatch.setattr(
        target_plugin_install, "_installed_plugin_metadata",
        lambda _root: {
            "plugin.json": {"name": target_plugin_install.PLUGIN_NAME},
            "package.json": {"version": None},
        },
    )

    with pytest.raises(RuntimeError, match="names no usable version") as refusal:
        target_plugin_install._live_authority_from_answers(matches, status)

    assert "read the authority again" not in str(refusal.value)


def _spacing_clock(monkeypatch, *, wall: float) -> list[float]:
    """Drive the spacing wait's clock, and collect what it sleeps."""
    slept: list[float] = []
    monkeypatch.setattr(target_plugin_install.time, "time", lambda: wall)
    monkeypatch.setattr(target_plugin_install.time, "sleep", lambda seconds: slept.append(seconds))
    return slept


def _reload_record(records: Path, name: str, **stamp: object) -> None:
    records.mkdir(parents=True, exist_ok=True)
    (records / name).write_text(json.dumps({"frontend_reload": stamp}), encoding="utf-8")


def test_two_installs_inside_a_minute_wait_out_deckys_webhelper_window(tmp_path, monkeypatch):
    """Decky stops its own service when the webhelper is replaced too often.

    Every install ends in a webhelper replacement, because that is how the exact
    frontend is imported. Decky counts one that lands within a minute of the
    previous as a crash and shuts the loader down on the third: on 2026-09-13
    three installs 27, 29 and 31 seconds apart did exactly that, and the device
    needed a service restart only root can give. `docs/FIELD_NOTES.md` carries
    the run. So an install that owes time waits it out first.
    """
    records = tmp_path / "records"
    _reload_record(records, "20260913T143200000000Z-abc.json", requested_at=1_700_000_000.0 - 20.0)
    slept = _spacing_clock(monkeypatch, wall=1_700_000_000.0)

    waited = target_plugin_install._wait_out_reload_spacing(records)

    assert slept == [pytest.approx(45.0, abs=0.1)]
    assert waited == pytest.approx(45.0, abs=0.1)


def test_an_install_that_owes_nothing_waits_for_nothing(tmp_path, monkeypatch):
    """Ten minutes is ten minutes, whatever else the record does or does not say.

    The window this is spacing against is Decky's, and Decky keeps it on the
    wall clock: `handle_crash` compares `time()` with the moment of the last
    webhelper exit. So a record that carries only that moment is not a record
    that has to be waited out, and charging one for being old cost every first
    install after a helper change a minute it did not owe.
    """
    records = tmp_path / "records"
    _reload_record(records, "20260913T143200000000Z-abc.json", requested_at=1_700_000_000.0 - 600.0)
    slept = _spacing_clock(monkeypatch, wall=1_700_000_000.0)

    assert target_plugin_install._wait_out_reload_spacing(records) == 0.0
    assert slept == []
    # And the first install on a device that has never held one waits too:
    # there is no moment to measure from, and nothing to be spaced against.
    assert target_plugin_install._wait_out_reload_spacing(tmp_path / "empty") == 0.0


@pytest.mark.parametrize("ahead", [60.0, 86_400.0, 10 * 365 * 86_400.0])
def test_a_moment_in_this_clocks_future_is_waited_out_once(tmp_path, monkeypatch, ahead):
    """An unmeasurable age is unknown, and unknown is not elapsed.

    It does not prove Decky's own window has cleared, and does not claim to:
    after a clock moved backwards Decky's `last_webhelper_exit` is in its future
    too, so it counts every replacement as a crash until wall time catches up,
    and nothing this helper can wait out changes that. One bounded window is
    what it does instead of pretending otherwise, and it is bounded rather than
    the distance back to the recorded moment, which a corrupt stamp would make
    a sleep with no end.
    """
    records = tmp_path / "records"
    _reload_record(records, "20260913T143200000000Z-abc.json", requested_at=1_700_000_000.0 + ahead)
    slept = _spacing_clock(monkeypatch, wall=1_700_000_000.0)

    spacing = target_plugin_install.WEBHELPER_RESTART_SPACING_SECONDS
    assert target_plugin_install._wait_out_reload_spacing(records) == pytest.approx(spacing)
    assert slept == [pytest.approx(spacing)]


def test_a_reload_whose_install_then_failed_still_spaces_the_next_one(tmp_path, monkeypatch):
    """A record is written for an install that worked; the window is about the reload.

    The webhelper is replaced before this helper can confirm anything, so an
    install that fails after that point leaves Decky's crash counter advanced
    and no record saying so. The next install would then not wait, which is how
    three replacements land inside a minute.
    """
    records = tmp_path / "records"
    target_plugin_install._note_reload_requested(records, 1_700_000_000.0 - 10.0)
    slept = _spacing_clock(monkeypatch, wall=1_700_000_000.0)

    assert target_plugin_install._wait_out_reload_spacing(records) == pytest.approx(55.0, abs=0.1)
    assert slept == [pytest.approx(55.0, abs=0.1)]
    # And the marker is not mistaken for an install record by the authority read.
    assert not list(records.glob("*.json"))


def test_a_stale_marker_never_hides_a_newer_install_record(tmp_path):
    """The marker and the record are two observations, and the newest wins.

    Writing the marker is allowed to fail quietly, so a readable but stale one
    can stand in front of a record written by an install that did reload. Taken
    as a primary source it declares Decky's window elapsed when it has not.
    """
    records = tmp_path / "records"
    records.mkdir()
    old, new = 1_700_000_000.0, 1_700_000_050.0
    target_plugin_install._note_reload_requested(records, old)
    _reload_record(records, "20260913T150000000000Z-abc.json", requested_at=new)

    # Exact, deliberately: `approx` defaults to a relative tolerance, and at
    # these clock values fifty seconds sits comfortably inside it, so the loose
    # assertion passed against the very code it was written to refuse.
    assert target_plugin_install.last_reload_request(records) == new


def test_a_marker_that_could_not_be_written_leaves_the_record_in_charge(tmp_path, monkeypatch):
    """The failure shape the quiet `OSError` in the marker writer implies."""
    records = tmp_path / "records"
    records.mkdir()
    target_plugin_install._note_reload_requested(records, 1_700_000_000.0)
    monkeypatch.setattr(
        target_plugin_install.Path, "write_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("read-only state directory")),
    )
    # The install reloads at the newer moment and cannot update the marker.
    target_plugin_install._note_reload_requested(records, 1_700_000_050.0)
    monkeypatch.undo()
    _reload_record(records, "20260913T150000000000Z-abc.json", requested_at=1_700_000_050.0)
    slept = _spacing_clock(monkeypatch, wall=1_700_000_060.0)

    assert target_plugin_install._wait_out_reload_spacing(records) == pytest.approx(55.0, abs=0.1)
    assert slept == [pytest.approx(55.0, abs=0.1)]


class _FakeSSH:
    """Every `ssh` invocation the remote install makes, and what it answered.

    The commands are kept as they were handed to `ssh`, because what this is
    about is what the remote shell is asked to run.
    """

    def __init__(self, digest: str, stdout: str = "installed", stderr: str = "") -> None:
        self.digest, self.stdout, self.stderr = digest, stdout, stderr
        self.commands: list[str] = []

    def __call__(self, argv, **kwargs):
        command = argv[-1]
        self.commands.append(command)
        if "hashlib" in command:
            return _Completed(self.digest.encode())
        if command.startswith("mkdir"):
            return _Completed(b"")
        return _Completed(self.stdout.encode(), self.stderr.encode())


class _Completed:
    def __init__(self, stdout: bytes, stderr: bytes = b"") -> None:
        self.stdout, self.stderr, self.returncode = stdout, stderr, 0


def _remote_install(monkeypatch, tmp_path: Path, ssh: _FakeSSH, **kwargs) -> str:
    package = tmp_path / "CE-Decky-v0.9.26.zip"
    package.write_bytes(b"not really a zip")
    monkeypatch.setattr(target_plugin_install.shutil, "which", lambda name: "/usr/bin/ssh")
    monkeypatch.setattr(target_plugin_install.subprocess, "run", ssh)
    return target_plugin_install.install_through_mirror(
        package, sha256(package.read_bytes()).hexdigest(),
        remote="deck@device", root="ce-decky-mirror", replace=True, timeout=30.0, **kwargs,
    )


def _helper_digest() -> str:
    return sha256(Path(target_plugin_install.__file__).resolve().read_bytes()).hexdigest()


def test_a_remote_path_with_a_space_in_it_arrives_as_one_argument(monkeypatch, tmp_path: Path):
    # `--plugin-root` points at a Decky plugin directory, and a device whose
    # home or library holds a space produces one with a space in it. Written
    # into the command as it stood, that path broke in half and everything after
    # it was left to the remote shell to interpret.
    ssh = _FakeSSH(_helper_digest())
    _remote_install(monkeypatch, tmp_path, ssh, plugin_root="/home/deck/My Plugins/CE-Decky")
    install = ssh.commands[-1]
    assert "'/home/deck/My Plugins/CE-Decky'" in install
    assert "/home/deck/My Plugins/CE-Decky" not in install.replace("'/home/deck/My Plugins/CE-Decky'", "")


def test_a_remote_path_cannot_add_a_command_of_its_own(monkeypatch, tmp_path: Path):
    ssh = _FakeSSH(_helper_digest())
    _remote_install(monkeypatch, tmp_path, ssh, plugin_root="/plugins/x; rm -rf ~")
    install = ssh.commands[-1]
    # Quoted whole, so the remote shell has one more argument rather than one
    # more command.
    assert "'/plugins/x; rm -rf ~'" in install
    assert "; rm -rf ~" not in install.replace("'/plugins/x; rm -rf ~'", "")


def test_the_options_this_accepts_reach_the_helper_that_does_the_work(monkeypatch, tmp_path: Path, capsys):
    # Both used to be accepted here and dropped on the way: a custom endpoint
    # was silently ignored, and `--remote --json` printed a summary line to a
    # caller parsing JSON.
    ssh = _FakeSSH(_helper_digest(), stdout='{"ok": true}\n', stderr="install record not written")
    answer = _remote_install(
        monkeypatch, tmp_path, ssh,
        decky_url="http://127.0.0.1:1337", as_json=True,
    )
    install = ssh.commands[-1]
    assert "--decky-url http://127.0.0.1:1337" in install
    assert "--json" in install
    # The answer is still a JSON document, and what the far end said about it is
    # relayed rather than folded into that document.
    assert json.loads(answer) == {"ok": True}
    assert "install record not written" in capsys.readouterr().err


def test_the_version_a_package_should_carry_reaches_the_far_end(monkeypatch, tmp_path: Path):
    # The one build that is deliberately older than the checkout is also the one
    # somebody drives from another machine, to test updating on a device they
    # are not sitting at. Dropped on the way, the far end compares the package
    # against its own version and refuses it.
    ssh = _FakeSSH(_helper_digest())
    _remote_install(monkeypatch, tmp_path, ssh, expect_version="0.9.26")
    assert "--expect-version 0.9.26" in ssh.commands[-1]


def test_a_summary_run_still_carries_what_the_far_end_warned_about(monkeypatch, tmp_path: Path):
    ssh = _FakeSSH(_helper_digest(), stdout="installed 0.9.26\n", stderr="install record not written")
    answer = _remote_install(monkeypatch, tmp_path, ssh)
    assert answer.splitlines() == ["installed 0.9.26", "install record not written"]
