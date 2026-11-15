from __future__ import annotations

import json
import re

import pytest

from scripts import host_platform
from scripts import target_device_timeline as timeline

T0 = 1_794_684_000.0  # a fixed moment; only differences matter


def entry(seconds: float, message: str, identifier: str = "PluginLoader") -> dict:
    return {
        "__REALTIME_TIMESTAMP": str(int((T0 + seconds) * 1_000_000)),
        "MESSAGE": message, "SYSLOG_IDENTIFIER": identifier, "_PID": "1",
    }


def oom(seconds: float, message: str) -> dict:
    return entry(seconds, message, "earlyoom")


def systemd(seconds: float, message: str) -> dict:
    return entry(seconds, message, "systemd")


def test_a_kill_carries_the_memory_left_its_escalation_and_its_exit_but_never_a_command_line():
    events = timeline.parse_earlyoom([
        oom(0, "mem avail:  7175 of 12658 MiB (56.68%), swap free: 7063 of 8947 MiB (78.94%)"),
        oom(1000, "mem avail:   292 of 13028 MiB ( 2.25%), swap free:    0 of 8947 MiB ( 0.00%)"),
        oom(1000, 'sending SIGTERM to process 1792 uid 0 "Decky Loader": oom_score 1148, oom_score_adj 0,'
                  ' VmRSS 12153 MiB, cmdline "/home/user/Games/secret-title/run --flag"'),
        oom(1000.1, "escalating to SIGKILL after 0.100 seconds"),
        oom(1001.6, "process 1792 exited after 1.600 seconds"),
    ], boot=-3)
    reading, kill = events
    assert reading.kind == "memory" and reading.fields["avail"] == 7175
    assert kill.kind == "oom" and kill.fields["signal"] == "SIGTERM→SIGKILL"
    assert kill.fields["available_mib"] == 292 and kill.fields["swap_free_mib"] == 0
    assert kill.text == (
        "earlyoom SIGTERM→SIGKILL Decky Loader (pid 1792, uid 0) at 12153 MiB"
        " · 292 MiB available, swap 0 MiB free · exited after 1.6s"
    )
    assert "secret-title" not in json.dumps([event.as_dict() for event in events])


def test_a_reading_long_before_a_kill_is_not_taken_for_the_moment_of_it():
    events = timeline.parse_earlyoom([
        oom(0, "mem avail:   900 of  8000 MiB (11%), swap free:  10 of 8947 MiB (0.1%)"),
        oom(30, 'sending SIGTERM to process 7 uid 1000 "steam": oom_score 1, oom_score_adj 0, VmRSS 28 MiB, cmdline "x"'),
    ], boot=0)
    assert [event.kind for event in events] == ["memory", "oom"]
    assert "available_mib" not in events[1].fields


def test_a_unit_stop_is_one_line_with_what_it_left_running_and_what_it_peaked_at():
    unit = "plugin_loader.service"
    events = timeline.parse_unit([
        systemd(0, f"{unit}: Main process exited, code=killed, status=9/KILL"),
        systemd(0, f"{unit}: Failed with result 'signal'."),
        systemd(0, f"{unit}: Unit process 2260 (PluginLoader) remains running after unit stopped."),
        systemd(0, f"{unit}: Unit process 2321 (CE Decky) remains running after unit stopped."),
        systemd(0, f"{unit}: Consumed 6min 8.432s CPU time over 9h 18min 42.559s wall clock time,"
                   " 12.9G memory peak, 6.8G memory swap peak."),
        systemd(0.1, f"{unit}: Scheduled restart job, restart counter is at 1."),
        systemd(0.2, f"{unit}: Found left-over process 2321 (CE Decky) in control group while starting unit. Ignoring."),
        systemd(0.2, "Started SteamDeck Plugin Loader."),
        # Another unit's records arrive in no query of this one, but a stray one changes nothing.
        systemd(0.3, "other.service: Main process exited, code=exited, status=0/SUCCESS"),
    ], boot=-3, unit=unit)
    stopped, restart, started = events
    assert stopped.name == "unit.stopped"
    assert stopped.text == (
        "plugin_loader stopped (main process killed, status 9/KILL), result signal;"
        " left running: PluginLoader 2260, CE Decky 2321 · memory peak 12.9G, swap peak 6.8G"
        " · ran 9h 18min 42.559s"
    )
    assert restart.fields["counter"] == 1
    assert started.text == "plugin_loader started; found left over from before: CE Decky 2321"


def test_an_ordinary_stop_and_start_read_as_such():
    unit = "plugin_loader.service"
    events = timeline.parse_unit([
        systemd(0, "Stopping SteamDeck Plugin Loader..."),
        systemd(2, f"{unit}: Consumed 1min CPU time, 300M memory peak."),
        systemd(2, "Stopped SteamDeck Plugin Loader."),
        systemd(3, "Started SteamDeck Plugin Loader."),
    ], boot=0, unit=unit)
    assert [event.text for event in events] == [
        "plugin_loader: stopping",
        "plugin_loader stopped · memory peak 300M",
        "plugin_loader started",
    ]


def test_decky_records_read_as_what_happened_and_a_plugin_s_own_output_is_left_out():
    events = timeline.collapse(timeline.parse_decky([
        entry(0, "[browser][INFO]: Installing Some Plugin from URL (Version: 0.8.0)"),
        entry(2, "[browser][INFO]: uninstalling Some Plugin"),
        entry(2, "[plugin][INFO]: Shutting down Some Plugin (v0.7.6)"),
        entry(7, "[plugin][WARNING]: Plugin Some Plugin (v0.7.6) still alive 5 seconds after stop request! Sending SIGKILL!"),
        entry(8, "[browser][INFO]: Installed Some Plugin (Version: 0.8.0)"),
        *[entry(10 + n * 0.1, "[loader][INFO]: Plugin Some Plugin (v0.8.0) is already loaded and has requested to not be re-loaded")
          for n in range(29)],
        entry(20, "[browser][INFO]: Installing CE Decky from local ZIP file (Version: 0.9.36)"),
        entry(30, "[main][WARNING]: webhelper crashed within a minute from last crash! crash count: 3"),
        entry(31, "[main][INFO]: Shutting down..."),
        entry(40, "[main][INFO]: Starting Decky version v3.3.1"),
        entry(41, "[loader][INFO]: Loaded Some Plugin (v0.8.0)"),
        entry(42, "[loader][INFO]: Loaded CE Decky (v0.9.36)"),
        entry(43, "[injector][WARNING]: The Tab SharedJSContext socket has been disconnected while listening for messages."),
        entry(44, "[SomePlugin][INFO]: a plugin's own record"),
        entry(44, "[_main] another plugin's print"),
        entry(44, "[loader][INFO]: plugin_path: /somewhere"),
    ], boot=0))
    texts = [event.text for event in events]
    assert texts == [
        "installing Some Plugin 0.8.0 from the store",
        "uninstalling Some Plugin (the old version, as part of a replacement or a removal)",
        "stopping Some Plugin (v0.7.6)",
        "Some Plugin (v0.7.6) still alive 5s after its stop request: SIGKILL",
        "installed Some Plugin 0.8.0",
        "refused to reload Some Plugin (v0.8.0), already loaded",
        "installing CE Decky 0.9.36 from a local ZIP",
        "Steam's webhelper crashed again within a minute: count 3 (Decky stops itself at 3)",
        "Decky shutting down",
        "Decky v3.3.1 starting",
        "loaded 2 plugins: Some Plugin 0.8.0, CE Decky 0.9.36",
        "[injector] The Tab SharedJSContext socket has been disconnected while listening for messages.",
    ]
    refused = events[5]
    assert refused.count == 29 and refused.until == pytest.approx(T0 + 12.8)


def test_crashes_and_the_kernel_s_own_oom_kills_are_read():
    crashes = timeline.parse_coredump([
        entry(0, "Process 2382 (gamescope-wl) of user 1000 terminated abnormally with signal 6/ABRT, processing...",
              "systemd-coredump"),
        entry(1, "Process 2382 (gamescope-wl) of user 1000 dumped core.", "systemd-coredump"),
    ], boot=-1)
    assert [event.text for event in crashes] == ["gamescope-wl (pid 2382) ended by 6/ABRT, core dumped"]
    kernel = timeline.parse_kernel([
        entry(0, "Out of memory: Killed process 2284 (Proton Pulse) total-vm:4768320kB, anon-rss:2941076kB,"
                 " file-rss:1648kB, shmem-rss:740kB, UID:0 pgtables:9236kB oom_score_adj:0", "kernel"),
        entry(1, "steam[4242]: segfault at 0 ip 0000 sp 0000 error 4 in libc.so.6", "kernel"),
        entry(2, "an unrelated kernel line", "kernel"),
    ], boot=-2)
    assert [event.text for event in kernel] == [
        "kernel OOM killer took Proton Pulse (pid 2284) at 2872 MiB anonymous",
        "steam (pid 4242) segfaulted",
    ]


def test_the_session_s_power_and_sleep_are_read():
    events = timeline.parse_logind([
        entry(0, "Power key pressed short.", "systemd-logind"),
        entry(1, "The system will suspend now!", "systemd-logind"),
        entry(2, "Operation 'suspend' finished.", "systemd-logind"),
        entry(3, "System is rebooting.", "systemd-logind"),
        entry(4, "New session 3 of user deck.", "systemd-logind"),
    ], boot=0)
    assert [event.name for event in events] == [
        "session.power_key", "session.suspend", "session.resume", "session.reboot",
    ]


def test_a_process_killed_over_and_over_is_one_line_naming_the_largest():
    lines = []
    for n, rss in enumerate((74, 478, 337)):
        lines.append(oom(n * 2, f'sending SIGTERM to process {100 + n} uid 1000 "steamwebhelper": oom_score 1,'
                                f' oom_score_adj 200, VmRSS {rss} MiB, cmdline "x"'))
    lines.append(oom(7, 'sending SIGTERM to process 9 uid 1000 "CE Decky": oom_score 1, oom_score_adj 0,'
                        ' VmRSS 3405 MiB, cmdline "x"'))
    events = timeline.collapse(timeline.parse_earlyoom(lines, boot=-2))
    helper, plugin = events
    assert helper.count == 3 and helper.text.endswith("the largest of them 478 MiB")
    assert plugin.count == 1 and plugin.fields["rss_mib"] == 3405


BOOTS = [
    {"index": -1, "boot_id": "a" * 32, "first_entry": int(T0 * 1e6), "last_entry": int((T0 + 100) * 1e6)},
    {"index": 0, "boot_id": "b" * 32, "first_entry": int((T0 + 200) * 1e6), "last_entry": int((T0 + 90_000) * 1e6)},
]


def fake_journal(sources: dict[str, list[dict]], *, refuse: str | None = None):
    calls: list[list[str]] = []

    def runner(arguments: list[str]) -> tuple[int, str, str]:
        calls.append(arguments)
        if arguments[:1] == ["--list-boots"]:
            return 0, json.dumps(BOOTS), ""
        # The source is the last argument before the output options: a unit or
        # identifier name, or `-k` for the kernel.
        name = arguments[arguments.index("-o") - 1]
        if refuse == name:
            return 0, "", "Hint: You are currently not seeing messages from other users and the system."
        return 0, "\n".join(json.dumps(item) for item in sources.get(name, [])), ""

    return runner, calls


def test_the_report_names_a_source_it_could_not_read_rather_than_calling_the_boot_quiet():
    runner, calls = fake_journal({
        "plugin_loader.service": [systemd(0, "Started SteamDeck Plugin Loader.")],
    }, refuse="earlyoom.service")
    boots = timeline.list_boots(runner)
    report = timeline.timeline(timeline.select_boots(boots, ["0"]), runner=runner)
    boot = report["boots"][0]
    assert boot["index"] == 0 and boot["counts"] == {"unit": 1}
    assert "not seeing messages" in boot["unreadable"]["earlyoom"]
    # Every read is bounded to the boot by its id, and to the fields this reads.
    reads = [call for call in calls if call[:1] == ["-b"]]
    assert reads and all(call[1] == "b" * 32 and f"--output-fields={timeline.FIELDS}" in call for call in reads)
    text = timeline.render(report)
    assert "   ! earlyoom: the journal could not be read in full by this user" in text
    # The boot ran into the next day, so its end says which.
    assert len(re.findall(r"\d{4}-\d{2}-\d{2}", text.splitlines()[0])) == 2


def test_only_the_kinds_asked_for_are_read_and_kept():
    runner, calls = fake_journal({"systemd-logind.service": [entry(0, "Power key pressed short.", "systemd-logind")]})
    report = timeline.timeline(timeline.select_boots(timeline.list_boots(runner), ["all"]), kinds=("session",),
                               runner=runner)
    assert [boot["counts"] for boot in report["boots"]] == [{"session": 1}, {"session": 1}]
    assert all("systemd-logind.service" in call for call in calls if call[:1] == ["-b"])


def test_a_boot_the_journal_does_not_hold_is_refused_by_name():
    boots = timeline.list_boots(fake_journal({})[0])
    with pytest.raises(ValueError, match="no boot -7"):
        timeline.select_boots(boots, ["-7"])
    with pytest.raises(ValueError, match="takes an index"):
        timeline.select_boots(boots, ["yesterday"])
    assert [boot.index for boot in timeline.select_boots(boots, [])] == [0]


def test_main_lists_boots_and_refuses_a_host_that_is_not_the_target(monkeypatch, capsys):
    runner, _calls = fake_journal({})
    monkeypatch.setattr(timeline.host_platform, "require", lambda *_a, **_k: None)
    assert timeline.main(["--list-boots"], runner=runner) == 0
    assert "b" * 32 in capsys.readouterr().out
    with pytest.raises(SystemExit):
        timeline.main(["--only", "nonsense"], runner=runner)

    def refuse(*_args, **_kwargs):
        raise host_platform.UnsupportedHost("not the target")

    monkeypatch.setattr(timeline.host_platform, "require", refuse)
    assert timeline.main(["--boot", "0"], runner=runner) == 3


def test_asking_for_crashes_alone_still_reads_the_kernel_s_segfaults():
    runner, calls = fake_journal({"-k": [entry(0, "steam[4242]: segfault at 0 ip 0 sp 0 error 4", "kernel")]})
    report = timeline.timeline(timeline.select_boots(timeline.list_boots(runner), ["0"]), kinds=("crash",),
                               runner=runner)
    assert [event["text"] for event in report["boots"][0]["events"]] == ["steam (pid 4242) segfaulted"]
    # And memory alone does not read the kernel at all.
    runner, calls = fake_journal({})
    timeline.timeline(timeline.select_boots(timeline.list_boots(runner), ["0"]), kinds=("memory",), runner=runner)
    assert not any("-k" in call for call in calls)
