from __future__ import annotations

from pathlib import Path

import pytest

from scripts import target_session_cost_probe as cost


def _stat_fields(utime: int, stime: int, starttime: int) -> list[str]:
    """Everything after the parenthesised comm, in its real /proc order.

    The first field here is the state, which is field 3 of the whole line, so
    field N of the line is at index N-3: utime 14, stime 15, starttime 22.
    """
    fields = ["0"] * 50
    fields[0] = "S"
    fields[11] = str(utime)
    fields[12] = str(stime)
    fields[19] = str(starttime)
    return fields


def _process(proc_root: Path, pid: int, comm: str, cmdline: str, ticks: int, starttime: int = 100) -> Path:
    entry = proc_root / str(pid)
    entry.mkdir(parents=True)
    utime = ticks // 2
    stime = ticks - utime
    (entry / "stat").write_text(f"{pid} ({comm}) " + " ".join(_stat_fields(utime, stime, starttime)) + "\n", encoding="utf-8")
    (entry / "comm").write_text(comm + "\n", encoding="utf-8")
    (entry / "cmdline").write_bytes(cmdline.replace(" ", "\0").encode("utf-8") + b"\0")
    return entry


def test_parses_a_comm_that_contains_spaces_and_parentheses():
    raw = "42 (weird (name) here) " + " ".join(_stat_fields(7, 3, 991))
    parsed = cost.parse_stat(raw)
    assert parsed is not None
    comm, ticks, starttime = parsed
    assert comm == "weird (name) here"
    assert ticks == 10
    assert starttime == 991


def test_refuses_a_stat_line_that_is_too_short():
    assert cost.parse_stat("42 (x) S 1 2 3") is None


def test_classifies_the_parts_a_session_is_made_of():
    def sample(comm: str, cmdline: str) -> cost.ProcSample:
        return cost.ProcSample(1, comm, cmdline, 0, 0)

    assert cost.classify(sample("cheatengine-x86_64.exe", ""), None, None) == "cheat_engine"
    assert cost.classify(sample("wineserver", ""), None, None) == "wineserver"
    assert cost.classify(sample("python3", "/home/deck/homebrew/plugins/CE-Decky/main.py"), None, None) == "plugin_backend"
    assert cost.classify(sample("PluginLoader", "/home/deck/homebrew/services/PluginLoader"), None, None) == "decky_loader"
    assert cost.classify(sample("gamescope", ""), None, None) == "gamescope"
    assert cost.classify(sample("game_linux64", "reaper SteamLaunch AppId=440 --"), 440, None) == "game"
    assert cost.classify(sample("game_linux64", ""), None, "game_linux64") == "game"
    assert cost.classify(sample("kwin", ""), None, None) == "other"


def test_a_game_is_named_by_its_appid_before_the_steam_fallback():
    # The reaper carries both the AppID and a Steam path, and the AppID is the
    # exact identity, so it must win.
    sample = cost.ProcSample(1, "reaper", "/home/deck/.steam/steam/reaper SteamLaunch AppId=440 --", 0, 0)
    assert cost.classify(sample, 440, None) == "game"


def test_reports_cpu_per_group_across_the_window(tmp_path: Path, monkeypatch):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    (proc_root / "stat").write_text("cpu  100 0 100 1000 0 0 0 0 0 0\n", encoding="utf-8")
    _process(proc_root, 10, "cheatengine-x86_64.exe", "cheatengine", 0)
    _process(proc_root, 11, "wineserver", "wineserver", 0)

    read_proc = cost.sample_processes
    samples = [read_proc(proc_root)]

    # The second sample is the first one advanced by an exact number of ticks,
    # so the arithmetic under test is the only thing the assertion depends on.
    def second_sample(_root: Path) -> dict[int, cost.ProcSample]:
        if samples:
            return samples.pop()
        later = read_proc(proc_root)
        later[10].ticks += cost.clock_ticks_per_second()
        later[11].ticks += cost.clock_ticks_per_second() // 2
        return later

    monkeypatch.setattr(cost, "sample_processes", second_sample)
    monkeypatch.setattr(cost.time, "sleep", lambda _seconds: None)
    clock = iter([0.0, 0.0, 1.0, 1.0, 1.0])
    monkeypatch.setattr(cost.time, "monotonic", lambda: next(clock))

    report = cost.probe(1.0, "unattached", None, None, None, 5, False, proc_root, read_sensors=False, read_counters=False)
    assert report["schema"] == 2
    assert report["label"] == "unattached"
    assert report["groups"]["cheat_engine"]["cpu_percent_of_core"] == 100.0
    assert report["groups"]["wineserver"]["cpu_percent_of_core"] == 50.0
    assert report["fps"]["error"] == "not read"
    # No total is published: adding the groups a session touches would count
    # work that is there either way, and read larger than attaching costs.
    assert not [key for key in report if key.startswith("session_cost")]


def test_a_process_that_restarted_inside_the_window_is_counted_nowhere(tmp_path: Path, monkeypatch):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    (proc_root / "stat").write_text("cpu  100 0 100 1000 0 0 0 0 0 0\n", encoding="utf-8")
    _process(proc_root, 10, "cheatengine-x86_64.exe", "cheatengine", 50, starttime=100)

    read_proc = cost.sample_processes
    first = read_proc(proc_root)
    calls = {"n": 0}

    def sampler(_root: Path) -> dict[int, cost.ProcSample]:
        calls["n"] += 1
        if calls["n"] == 1:
            return first
        # Same PID, different start time: a PID that was reused is a different
        # process, and its accumulated ticks say nothing about this window.
        later = read_proc(proc_root)
        later[10].starttime = 900
        later[10].ticks = 5000
        return later

    monkeypatch.setattr(cost, "sample_processes", sampler)
    monkeypatch.setattr(cost.time, "sleep", lambda _seconds: None)
    clock = iter([0.0, 0.0, 1.0, 1.0, 1.0])
    monkeypatch.setattr(cost.time, "monotonic", lambda: next(clock))

    report = cost.probe(1.0, "attached", None, None, None, 5, False, proc_root, read_sensors=False, read_counters=False)
    assert report["groups"] == {}
    assert report["processes_appeared_in_window"] == 1


def test_every_wineserver_on_the_host_is_counted_and_the_count_says_so(tmp_path: Path, monkeypatch):
    # Nothing here proves which prefix a wineserver belongs to, so an unrelated
    # Wine workload lands in the same group. The report must not hide that: the
    # group's process count is what tells the reader the group is not
    # attributable to the session under test.
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    (proc_root / "stat").write_text("cpu  100 0 100 1000 0 0 0 0 0 0\n", encoding="utf-8")
    _process(proc_root, 10, "wineserver", "wineserver", 0)
    _process(proc_root, 11, "wineserver", "wineserver", 0)

    read_proc = cost.sample_processes
    samples = [read_proc(proc_root)]

    def sampler(_root: Path) -> dict[int, cost.ProcSample]:
        if samples:
            return samples.pop()
        later = read_proc(proc_root)
        later[10].ticks += cost.clock_ticks_per_second()
        later[11].ticks += cost.clock_ticks_per_second()
        return later

    monkeypatch.setattr(cost, "sample_processes", sampler)
    monkeypatch.setattr(cost.time, "sleep", lambda _seconds: None)
    clock = iter([0.0, 0.0, 1.0, 1.0, 1.0])
    monkeypatch.setattr(cost.time, "monotonic", lambda: next(clock))

    report = cost.probe(1.0, "unattached", None, None, None, 5, False, proc_root, read_sensors=False, read_counters=False)
    assert report["groups"]["wineserver"]["processes"] == 2
    assert report["groups"]["wineserver"]["cpu_percent_of_core"] == 200.0

def test_discovers_the_statistics_pipe_from_the_live_compositor(tmp_path: Path):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _process(proc_root, 7, "kwin", "kwin", 0)
    # SteamOS names the compositor process "gamescope-wl", which is the exact
    # spelling that made a first version of this probe find nothing at all.
    _process(
        proc_root,
        8,
        "gamescope-wl",
        "gamescope -e -R /run/user/1000/gamescope.live/startup.socket -T /run/user/1000/gamescope.live/stats.pipe",
        0,
    )
    assert cost.discover_stats_pipe(proc_root) == "/run/user/1000/gamescope.live/stats.pipe"


def test_the_session_launcher_script_is_not_the_compositor(tmp_path: Path):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _process(proc_root, 7, "start-gamescope", "/bin/sh /usr/bin/start-gamescope-session -T /wrong/stats.pipe", 0)
    assert cost.discover_stats_pipe(proc_root) is None


def test_no_compositor_means_no_pipe_rather_than_a_guess(tmp_path: Path):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _process(proc_root, 7, "kwin", "kwin", 0)
    assert cost.discover_stats_pipe(proc_root) is None


def test_summarises_frames_and_drops_the_partial_first_second():
    reader = cost.FpsReader("/dev/null", 0.0)
    for line in ("fps=0.29", "focus=steam", "fps=60.0", "focus=steam", "fps=58.0", "focus=steam", "not a field"):
        reader._consume(line)
    summary = reader.summary()
    assert summary["samples"] == 3
    assert summary["usable_samples"] == 2
    assert summary["partial_samples"] == 1
    assert summary["mean"] == 59.0
    assert summary["focus_seen"] == ["steam"]


def test_a_real_stall_is_kept_however_small_it_reads():
    # A window that spent its time below one frame a second is the observation
    # this exists to make, and an earlier version dropped it for being small.
    reader = cost.FpsReader("/dev/null", 0.0)
    for line in ("fps=30.0", "focus=g", "fps=0.4", "focus=g", "fps=0.2", "focus=g", "fps=0.9", "focus=g"):
        reader._consume(line)
    summary = reader.summary()
    assert summary["usable_samples"] == 3
    assert summary["min"] == 0.2
    assert summary["max"] == 0.9
    assert summary["mean"] == 0.5


def test_the_reading_that_spans_a_focus_change_is_the_one_dropped():
    # The rate is written before the focus it belongs to, so the pair whose
    # focus differs from the pair before it is the one covering two things.
    reader = cost.FpsReader("/dev/null", 0.0)
    for line in ("fps=60.0", "focus=steam", "fps=59.0", "focus=steam", "fps=9.1", "focus=440", "fps=38.0", "focus=440"):
        reader._consume(line)
    summary = reader.summary()
    assert summary["samples"] == 4
    assert summary["usable_samples"] == 2
    assert summary["mean"] == 48.5
    assert summary["focus_seen"] == ["steam", "440"]


def test_frames_report_stays_readable_when_the_pipe_gave_nothing():
    reader = cost.FpsReader("/dev/null", 0.0)
    summary = reader.summary()
    assert summary["usable_samples"] == 0
    assert "mean" not in summary


def test_window_must_be_positive(tmp_path: Path):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    try:
        cost.probe(0.0, "x", None, None, None, 5, False, proc_root, read_sensors=False, read_counters=False)
    except ValueError as exc:
        assert "positive" in str(exc)
    else:  # pragma: no cover - the probe must refuse a zero window
        raise AssertionError("a zero window must be refused")


def test_the_clock_tick_is_read_on_demand_so_the_module_imports_anywhere():
    # `os.sysconf` does not exist on Windows. A module-level lookup fails on the
    # import line, where nothing can report an unsupported host, so the value
    # has to stay behind a call.
    assert "CLOCK_TICKS" not in vars(cost)
    assert cost.clock_ticks_per_second() > 0


def test_the_backends_denied_byte_counters_are_recorded_rather_than_dropped(tmp_path: Path):
    proc_root = tmp_path / "proc"
    (proc_root / "7").mkdir(parents=True)
    (proc_root / "7" / "status").write_text(
        "Name:\tCE Decky\nThreads:\t5\nVmRSS:\t  78112 kB\n"
        "voluntary_ctxt_switches:\t120\nnonvoluntary_ctxt_switches:\t4\n",
        encoding="utf-8",
    )
    (proc_root / "7" / "schedstat").write_text("900 250 12\n", encoding="utf-8")

    detail = cost.read_process_detail(proc_root, 7)

    assert detail["vm_rss_kb"] == 78112
    assert detail["threads"] == 5
    assert detail["voluntary_ctxt_switches"] == 120
    assert detail["runqueue_delay_ns"] == 250
    # No io file at all is the same shape of answer as a denied one: the field
    # says why, so a reader never mistakes it for a counter that stood still.
    assert "io_error" in detail
    assert "rchar" not in detail


def test_the_byte_counters_are_read_where_the_kernel_allows_them(tmp_path: Path):
    proc_root = tmp_path / "proc"
    (proc_root / "8").mkdir(parents=True)
    (proc_root / "8" / "status").write_text("VmRSS:\t 1024 kB\n", encoding="utf-8")
    (proc_root / "8" / "io").write_text("rchar: 500\nwchar: 20\nsyscr: 9\nsyscw: 2\n", encoding="utf-8")

    detail = cost.read_process_detail(proc_root, 8)

    assert detail["rchar"] == 500
    assert detail["syscr"] == 9
    assert "io_error" not in detail


def test_pressure_is_reported_as_the_delta_across_the_window(tmp_path: Path):
    path = tmp_path / "cpu"
    path.write_text("some avg10=0.00 avg60=0.00 avg300=0.00 total=1000\n", encoding="utf-8")
    first = cost.read_pressure(path)
    path.write_text("some avg10=0.00 avg60=0.00 avg300=0.00 total=1750\n", encoding="utf-8")
    second = cost.read_pressure(path)

    assert cost._pressure_delta(first, second) == {"some_total_us_delta": 750}
    assert "error" in cost._pressure_delta(None, second)


def test_sensor_channels_are_found_by_their_labels_not_by_an_assumed_path(tmp_path: Path):
    hwmon = tmp_path / "hwmon" / "hwmon3"
    hwmon.mkdir(parents=True)
    (hwmon / "name").write_text("amdgpu\n", encoding="utf-8")
    (hwmon / "power1_label").write_text("PPT\n", encoding="utf-8")
    (hwmon / "power1_average").write_text("1200000\n", encoding="utf-8")
    (hwmon / "freq1_label").write_text("sclk\n", encoding="utf-8")
    (hwmon / "freq1_input").write_text("800000000\n", encoding="utf-8")
    (hwmon / "temp1_input").write_text("36000\n", encoding="utf-8")
    card = tmp_path / "drm" / "card0" / "device"
    card.mkdir(parents=True)
    (card / "gpu_busy_percent").write_text("42\n", encoding="utf-8")

    channels = cost.discover_sensors(tmp_path / "hwmon", tmp_path / "drm", tmp_path / "nocpu")

    assert set(channels) == {
        "amdgpu_ppt_power_uw", "amdgpu_sclk_hz", "amdgpu_temp1_mc", "card0_gpu_busy_percent",
    }
    reader = cost.SensorReader(channels, deadline=0.0)
    reader.readings["amdgpu_ppt_power_uw"] = [1000000, 2000000]
    assert reader.summary()["amdgpu_ppt_power_uw"] == {
        "samples": 2, "mean": 1500000.0, "min": 1000000, "max": 2000000,
    }


def test_counter_deltas_use_the_backends_own_clock_for_the_window():
    before = {"schema": 1, "incarnation": "abc123", "uptime_seconds": 100.0, "paths": {
        "supervisor.game_target_state": {"calls": 10, "cpu_seconds": 0.10, "wall_seconds": 0.20},
    }}
    after = {"schema": 1, "incarnation": "abc123", "uptime_seconds": 130.0, "paths": {
        "supervisor.game_target_state": {"calls": 20, "cpu_seconds": 0.40, "wall_seconds": 0.50},
    }}

    delta = cost.poll_counter_deltas(before, after)

    assert delta["backend_window_seconds"] == 30.0
    row = delta["paths"]["supervisor.game_target_state"]
    assert row["calls"] == 10
    assert row["calls_per_second"] == pytest.approx(0.3333, abs=1e-4)
    assert row["cpu_percent_of_core"] == pytest.approx(1.0, abs=1e-6)


def test_a_backend_that_restarted_inside_the_window_reports_that_instead_of_a_delta():
    before = {"schema": 1, "incarnation": "first", "uptime_seconds": 900.0,
              "paths": {"rpc.get_runtime_status": {"calls": 300}}}
    after = {"schema": 1, "incarnation": "second", "uptime_seconds": 4.0,
             "paths": {"rpc.get_runtime_status": {"calls": 1}}}

    delta = cost.poll_counter_deltas(before, after)

    assert "restarted" in str(delta["error"])
    assert "paths" not in delta


def test_a_restart_that_leaves_the_uptime_rising_is_still_a_restart():
    """The case a clock alone cannot see, and the reason for the token.

    A snapshot five seconds after load, a restart, and a second snapshot eighty
    seconds into the new process read exactly like an ordinary ninety-second
    window. Subtracting them mixes two incarnations and can produce numbers that
    look entirely plausible, which is worse than a refusal.
    """
    before = {"schema": 1, "incarnation": "first", "uptime_seconds": 5.0,
              "paths": {"rpc.get_runtime_status": {"calls": 2, "cpu_seconds": 0.01, "wall_seconds": 0.02}}}
    after = {"schema": 1, "incarnation": "second", "uptime_seconds": 85.0,
             "paths": {"rpc.get_runtime_status": {"calls": 28, "cpu_seconds": 0.30, "wall_seconds": 0.40}}}

    delta = cost.poll_counter_deltas(before, after)

    assert "restarted" in str(delta["error"])
    assert "paths" not in delta


def test_a_backend_that_cannot_say_which_run_it_is_gets_no_delta():
    """An installed build older than the token cannot prove it stayed up."""
    before = {"schema": 1, "uptime_seconds": 100.0, "paths": {"rpc.get_runtime_status": {"calls": 1}}}
    after = {"schema": 1, "uptime_seconds": 190.0, "paths": {"rpc.get_runtime_status": {"calls": 31}}}

    delta = cost.poll_counter_deltas(before, after)

    assert "which run" in str(delta["error"])
    assert "paths" not in delta


def test_a_counter_schema_this_probe_does_not_read_is_reported_rather_than_guessed():
    before = {"schema": 99, "incarnation": "a", "uptime_seconds": 1.0, "paths": {}}
    after = {"schema": 99, "incarnation": "a", "uptime_seconds": 2.0, "paths": {}}

    delta = cost.poll_counter_deltas(before, after)

    assert "counter schema 99" in str(delta["error"])


def test_the_live_backend_snapshot_is_shaped_the_way_this_probe_subtracts_it():
    """The probe and the backend agree, or one of them is guessing."""
    from ce_decky import poll_counters

    snapshot = poll_counters.snapshot()

    assert snapshot["schema"] == cost.COUNTER_SCHEMA
    assert isinstance(snapshot["incarnation"], str) and snapshot["incarnation"]
    assert isinstance(snapshot["uptime_seconds"], float)


def test_an_unreachable_backend_is_a_note_rather_than_a_lost_window():
    delta = cost.poll_counter_deltas({"error": "not read"}, {"error": "not read"})

    assert delta == {"before": {"error": "not read"}, "after": {"error": "not read"}}


def test_a_slow_counter_read_costs_its_own_time_and_not_the_window(tmp_path: Path, monkeypatch):
    """The window is one window, or the report should not claim it is.

    The opening counter read talks to Decky over a socket with its own timeout.
    Anchoring the window before it meant that a slow or unreachable backend
    shortened the process-CPU sample by however long the call took while the
    frame and sensor readers kept the whole of it, so the readings the report
    presents as one window described different seconds. That is worst exactly
    when Decky is struggling, which is when the correlation matters most.
    """
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    (proc_root / "stat").write_text("cpu  100 0 100 1000 0 0 0 0 0 0\n", encoding="utf-8")
    _process(proc_root, 10, "cheatengine-x86_64.exe", "cheatengine", 0)

    elapsed = {"now": 0.0}
    slept: list[float] = []

    def monotonic() -> float:
        return elapsed["now"]

    def slow_read(_url: str, _timeout: float) -> dict[str, object]:
        # Five seconds of an unresponsive backend, before the anchor.
        elapsed["now"] += 5.0
        return {"error": "TimeoutError: timed out"}

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        elapsed["now"] += seconds

    monkeypatch.setattr(cost.time, "monotonic", monotonic)
    monkeypatch.setattr(cost.time, "sleep", sleep)
    monkeypatch.setattr(cost, "read_poll_counters", slow_read)
    monkeypatch.setattr(cost, "discover_sensors", dict)

    report = cost.probe(30.0, "attached", None, None, None, 5, False, proc_root, read_sensors=True)

    # The requested interval is still the requested interval.
    assert slept == [30.0]
    assert report["window_seconds"] == 30.0
    # And the delay is reported rather than folded into the window it precedes.
    assert report["counter_rpc_seconds"] == 5.0
    assert report["sampling_offset_seconds"] == 0.0


def test_the_pipes_own_cadence_is_measured_rather_than_assumed():
    """The compositor does not write once a second, whatever the old comment said.

    That assumption made every frame figure look like it rested on ninety
    readings when it rested on a dozen, so the interval between writes is now
    part of the report and a rate can never be read without it.
    """
    reader = cost.FpsReader("/nowhere", deadline=0.0)
    reader.samples = [
        [60.0, "app", 100.0],
        [59.0, "app", 105.3],
        [58.0, "app", 110.6],
        [61.0, "app", 118.1],
    ]

    report = reader.summary()

    assert report["write_interval_seconds"] == {"mean": 6.03, "min": 5.3, "max": 7.5}
    # The first reading is a window boundary and is never counted.
    assert report["usable_samples"] == 3


def test_a_spread_over_too_few_readings_says_so_instead_of_looking_solid():
    reader = cost.FpsReader("/nowhere", deadline=0.0)
    reader.samples = [[60.0, "app", 0.0], [59.0, "app", 6.0], [58.0, "app", 12.0]]

    report = reader.summary()

    assert report["mean"] == 58.5
    assert "the spread does not" in report["spread_warning"]

    # Enough readings and the warning is absent rather than always attached.
    reader.samples = [[60.0, "app", float(i * 6)] for i in range(8)]
    assert "spread_warning" not in reader.summary()


def test_the_open_timeout_outlasts_more_than_one_write_at_the_slowest_cadence():
    # Five seconds was shorter than a single write interval, so a live pipe
    # could be called dead for the ordinary reason that it had not written yet.
    assert cost.STATS_OPEN_TIMEOUT_S > cost.OBSERVED_STATS_INTERVAL_S


def test_a_last_reading_whose_focus_never_arrived_is_partial():
    """The compositor writes the rate first and the focus milliseconds later.

    A window that ends between the two leaves the final reading unpaired, and an
    unpaired reading cannot be placed against the focus before it. Counting it
    would let a focus change through at the one boundary nothing else catches.
    """
    reader = cost.FpsReader("/nowhere", deadline=0.0)
    reader.samples = [
        [60.0, "app", 0.0],
        [60.0, "app", 5.0],
        [60.0, "app", 10.0],
        [1.0, None, 15.0],
    ]

    report = reader.summary()

    assert report["usable_samples"] == 2
    assert report["partial_samples"] == 2
    # The unpaired reading is nowhere in the figures.
    assert report["mean"] == 60.0
    assert report["min"] == 60.0 and report["max"] == 60.0


def test_the_window_is_the_interval_between_the_two_process_samples(monkeypatch, tmp_path: Path):
    """The anchors sit at the samples, not after the reads that follow them.

    Measuring the window from after the detail reads put their duration into the
    denominator of every CPU percentage, which understated all of them by
    however long one pass over procfs took.
    """
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    (proc_root / "stat").write_text("cpu  100 0 100 1000 0 0 0 0 0 0\n", encoding="utf-8")
    _process(proc_root, 10, "cheatengine-x86_64.exe", "cheatengine", 0)

    clock = {"now": 0.0}
    monkeypatch.setattr(cost.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(cost.time, "sleep", lambda seconds: clock.__setitem__("now", clock["now"] + seconds))
    monkeypatch.setattr(cost, "discover_sensors", dict)

    real_detail = cost.read_process_detail

    def slow_detail(root, pid):
        # A pass over procfs that takes real time, on both ends of the window.
        clock["now"] += 2.0
        return real_detail(root, pid)

    monkeypatch.setattr(cost, "read_process_detail", slow_detail)

    report = cost.probe(30.0, "attached", None, None, None, 5, False, proc_root, read_counters=False)

    # 30 seconds between the samples, whatever the reads around them cost.
    assert report["window_seconds"] == 30.0


def test_the_counter_interval_brackets_the_window_and_says_by_how_much(monkeypatch, tmp_path: Path):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    (proc_root / "stat").write_text("cpu  100 0 100 1000 0 0 0 0 0 0\n", encoding="utf-8")
    _process(proc_root, 10, "cheatengine-x86_64.exe", "cheatengine", 0)

    clock = {"now": 0.0}
    monkeypatch.setattr(cost.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(cost.time, "sleep", lambda seconds: clock.__setitem__("now", clock["now"] + seconds))
    monkeypatch.setattr(cost, "discover_sensors", dict)

    def counter_read(_url, _timeout):
        clock["now"] += 1.5
        return {"error": "not read"}

    monkeypatch.setattr(cost, "read_poll_counters", counter_read)

    report = cost.probe(30.0, "attached", None, None, None, 5, False, proc_root)

    offsets = report["counter_window_offsets_seconds"]
    # The counters cannot be read inside the window, so the report says how far
    # outside each end they sit rather than calling it one window.
    assert offsets["before_window"] >= 0.0
    assert offsets["after_window"] == 1.5
    assert report["window_seconds"] == 30.0


def test_a_slow_closing_counter_read_stays_out_of_every_local_pair(monkeypatch, tmp_path: Path):
    """Nothing slow may sit between the two halves of a paired reading.

    The closing counter read talks to Decky with a five-second timeout. Taken
    between the process sample and the system, pressure and detail reads, that
    wait landed inside their numerators while the window they are divided by
    knew nothing about it, so an unreachable backend could overstate system CPU
    and stretch the pressure and per-process spans past the declared window.
    """
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _process(proc_root, 10, "cheatengine-x86_64.exe", "cheatengine", 0)

    clock = {"now": 0.0}
    busy = {"ticks": 0}

    def total_cpu_ticks(_root):
        # The system counter advances with the clock, so any wait folded into
        # this read shows up as system CPU that the window cannot explain.
        return int(clock["now"] * 100)

    monkeypatch.setattr(cost.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(cost.time, "sleep", lambda seconds: clock.__setitem__("now", clock["now"] + seconds))
    monkeypatch.setattr(cost, "discover_sensors", dict)
    monkeypatch.setattr(cost, "total_cpu_ticks", total_cpu_ticks)

    def slow_counter_read(_url, _timeout):
        clock["now"] += 5.0
        return {"error": "TimeoutError: timed out"}

    monkeypatch.setattr(cost, "read_poll_counters", slow_counter_read)

    report = cost.probe(30.0, "attached", None, None, None, 5, False, proc_root)

    assert report["window_seconds"] == 30.0
    # 30 seconds of a fully busy core over a 30-second window, and not 35.
    assert report["system_busy_percent_of_core"] == 100.0
    assert report["counter_window_offsets_seconds"]["after_window"] == 5.0
    assert busy["ticks"] == 0
