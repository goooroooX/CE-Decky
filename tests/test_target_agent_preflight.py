from __future__ import annotations

from pathlib import Path

from scripts import target_agent_preflight


def test_preflight_uses_a_bounded_workspace_write_roundtrip(tmp_path: Path):
    report = target_agent_preflight.probe(root=tmp_path)

    checks = {item["name"]: item for item in report["checks"]}
    assert checks["workspace_write"]["ok"] is True
    assert not list((tmp_path / "build").glob(".target-agent-preflight-*"))
    assert report["network_requested"] is False
    assert "network_origin" not in checks


def test_preflight_network_probe_is_explicit(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        target_agent_preflight,
        "_network_remote",
        lambda _root: target_agent_preflight.Check("network_origin", True, "fixture"),
    )

    report = target_agent_preflight.probe(network=True, root=tmp_path)

    checks = {item["name"]: item for item in report["checks"]}
    assert checks["network_origin"] == {"name": "network_origin", "ok": True, "detail": "fixture"}


def _host(**overrides):
    from scripts import host_platform

    fields = dict(
        system="Linux", family="linux", machine="x86_64", python_version="3.13.5",
        os_id="steamos", os_variant_id="steamdeck", os_version_id="3.8.16", os_build_id="20260716.1",
        steamos=True, vendor="Valve", product="Fremont", device="steam_machine", procfs=True,
    )
    fields.update(overrides)
    return host_platform.Host(**fields)


def test_a_linux_desktop_can_host_helpers_and_is_still_not_the_target(monkeypatch, tmp_path: Path):
    """The answer a desktop must give, and the one it must not.

    An ordinary Linux desktop has a process table, so helpers run on it. It is
    not the device, and reporting it as one would leave every claim about Steam,
    Proton and Cheat Engine looking answerable from a machine that cannot answer
    them.
    """
    monkeypatch.setattr(
        target_agent_preflight.host_platform, "describe",
        lambda: _host(os_id="arch", os_variant_id=None, steamos=False,
                      vendor="ASUS", product="Desktop", device=None),
    )
    monkeypatch.setattr(target_agent_preflight, "_git_checkout",
                        lambda _root: target_agent_preflight.Check("git_checkout", True, "fixture"))
    monkeypatch.setattr(target_agent_preflight, "REQUIRED_COMMANDS", ())

    report = target_agent_preflight.probe(root=tmp_path)

    assert report["repository_ok"] is True
    assert report["target_host_ok"] is True
    assert report["target_ok"] is False
    assert report["ok"] is False
    assert "not the target device" in report["notes"][0]
    assert "REMOTE_TARGET" in report["notes"][0]


def test_the_preflight_names_the_machine_it_is_running_on(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(target_agent_preflight.host_platform, "describe", lambda: _host())

    report = target_agent_preflight.probe(root=tmp_path)

    # The first report of a run has to be able to state this, and inferring it
    # from a path or a user name is what the helper exists to replace.
    assert report["host"]["steam_machine"] is True
    assert report["host"]["device"] == "steam_machine"
    assert report["target_host_ok"] is True
    assert report["target_ok"] is True


def test_a_windows_host_still_reports_the_repository_half_it_can_actually_do(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        target_agent_preflight.host_platform, "describe",
        lambda: _host(system="Windows", family="windows", os_id=None, steamos=False,
                      vendor=None, product=None, device=None, procfs=False),
    )
    monkeypatch.setattr(target_agent_preflight, "_git_checkout",
                        lambda _root: target_agent_preflight.Check("git_checkout", True, "fixture"))
    monkeypatch.setattr(target_agent_preflight, "REQUIRED_COMMANDS", ())

    report = target_agent_preflight.probe(root=tmp_path)

    assert report["target_ok"] is False
    assert report["target_host_ok"] is False
    assert report["repository_ok"] is True
    assert report["ok"] is False
    # The refusal has to say what this host is and where the target work goes,
    # rather than leaving an agent to read two failed checks and guess.
    assert "Windows" in report["notes"][0]
    assert "target work belongs on the device" in report["notes"][0]


def test_the_wrong_machine_exits_differently_from_a_checkout_that_is_not_ready(monkeypatch, capsys, tmp_path):
    from scripts import host_platform

    monkeypatch.setattr(target_agent_preflight, "ROOT", tmp_path)
    monkeypatch.setattr(
        target_agent_preflight.host_platform, "describe",
        lambda: _host(system="Windows", family="windows", procfs=False),
    )
    monkeypatch.setattr(target_agent_preflight, "_git_checkout",
                        lambda _root: target_agent_preflight.Check("git_checkout", True, "fixture"))
    monkeypatch.setattr(target_agent_preflight, "REQUIRED_COMMANDS", ())

    assert target_agent_preflight.main([]) == host_platform.EXIT_UNSUPPORTED_HOST
    capsys.readouterr()

    monkeypatch.setattr(target_agent_preflight, "_git_checkout",
                        lambda _root: target_agent_preflight.Check("git_checkout", False, "not a worktree"))
    assert target_agent_preflight.main([]) == 2
