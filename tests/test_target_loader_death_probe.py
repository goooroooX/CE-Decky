from __future__ import annotations

from pathlib import Path

import pytest

from scripts import host_platform
from scripts import target_loader_death_harness as harness
from scripts import target_loader_death_probe as probe


def _write_stat(root: Path, pid: int, *, ppid: int, state: str = "S", utime: int = 5, stime: int = 2) -> None:
    fields = [state, str(ppid)] + ["0"] * 9 + [str(utime), str(stime)] + ["0"] * 20
    (root / str(pid)).mkdir(parents=True, exist_ok=True)
    (root / str(pid) / "stat").write_text(f"{pid} (CE Decky) " + " ".join(fields) + "\n")


def test_read_stat_takes_state_parent_and_ticks_past_a_name_with_spaces(tmp_path: Path):
    _write_stat(tmp_path, 63040, ppid=63039, utime=40, stime=7)
    assert probe.read_stat(63040, tmp_path) == {"state": "S", "ppid": 63039, "ticks": 47}
    assert probe.read_stat(1, tmp_path) is None


def _report(**phases):
    return {
        "package_version": "0.9.36", "runtime_sha256": "7bb73b0502c49e7194f34f9c",
        "started": {"watch_armed": True},
        "forkserver_killed": {"plugin_alive": True, "lost_logged": False},
        **phases,
    }


def test_a_package_that_stays_for_the_forkserver_and_leaves_after_the_loader_passes():
    ok, line = probe.verdict(_report(loader_killed={
        "plugin_exited_after_s": 0.9, "lost_logged": True, "rss_first_mib": 63, "rss_last_mib": 201,
    }))
    assert ok is True
    assert line == (
        "target loader death probe: CE Decky 0.9.36 · Decky runtime 7bb73b0502c4 · watch armed"
        " · forkserver alone: stayed · loader killed: left after 0.9s with backend.loader_lost,"
        " RSS 63→201 MiB"
    )


def test_the_control_that_never_leaves_fails_and_says_what_it_cost():
    report = _report(loader_killed={
        "plugin_exited_after_s": None, "waited_s": 10.04, "lost_logged": False,
        "rss_first_mib": 63, "rss_last_mib": 1202, "cpu_share": 0.98,
    })
    report["started"] = {"watch_armed": False}
    ok, line = probe.verdict(report)
    assert ok is False
    assert "no watch" in line
    assert "still running after 10.04s, RSS 63→1202 MiB, 98% of a core, killed by the probe" in line


@pytest.mark.parametrize(
    ("phase_a", "phase_b"),
    [
        # Leaving when only the forkserver died closes the socket under a living loader.
        ({"plugin_alive": False, "lost_logged": True}, {"plugin_exited_after_s": 0.5, "lost_logged": True}),
        # Leaving without saying why is a process that died of something else.
        ({"plugin_alive": True, "lost_logged": False}, {"plugin_exited_after_s": 0.5, "lost_logged": False}),
    ],
)
def test_leaving_for_the_wrong_reason_or_silently_is_not_a_pass(phase_a, phase_b):
    ok, _line = probe.verdict(_report(forkserver_killed=phase_a, loader_killed=phase_b))
    assert ok is False


def test_nothing_is_signalled_once_a_pid_is_no_longer_the_process_identified(monkeypatch):
    sent: list[int] = []
    monkeypatch.setattr(probe.os, "kill", lambda pid, _sig: sent.append(pid))
    monkeypatch.setattr(probe, "read_stat", lambda _pid: {"state": "S", "ppid": 999, "ticks": 0})
    monkeypatch.setattr(probe, "_comm", lambda _pid: "PluginLoader")
    with pytest.raises(RuntimeError, match="nothing was signalled"):
        probe._kill_checked(63039, comm="PluginLoader", parent=63032)
    monkeypatch.setattr(probe, "_comm", lambda _pid: "bash")
    with pytest.raises(RuntimeError):
        probe._kill_checked(63039, comm="PluginLoader", parent=999)
    assert sent == []


def test_a_host_that_is_not_the_target_is_refused_before_anything_runs(monkeypatch, tmp_path: Path):
    def refuse(_what):
        raise host_platform.UnsupportedHost("not the target")

    monkeypatch.setattr(probe.host_platform, "require", refuse)
    monkeypatch.setattr(probe, "probe", lambda *_a, **_k: pytest.fail("probe ran on a refused host"))
    assert probe.main([str(tmp_path / "x.zip"), "--sha256", "0" * 64]) == 3


def test_the_harness_imports_without_decky_and_defines_nothing():
    # The subclass only exists inside Decky's runtime; anywhere else the module is inert.
    assert harness.SandboxedPlugin is None
    assert not hasattr(harness, "HomedSandboxedPlugin")
    assert harness.HOME_VARIABLE == probe.HOME_VARIABLE
