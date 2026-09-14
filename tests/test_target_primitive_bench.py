"""The tracked harness behind any per-call figure this project records.

The timings this replaces were written in a scratch directory on the device and
disappeared with it, leaving numbers nobody could reproduce. Every case here is
one property that makes a figure from this helper checkable by somebody who was
not at the machine.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts import target_primitive_bench as bench


def test_the_percentile_is_a_rank_so_seven_repetitions_can_carry_one():
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]

    # Nearest rank, never interpolated: a reader can find the answer in the list.
    assert bench._percentile(values, 0.95) == 7.0
    assert bench._percentile(values, 0.5) == 4.0
    assert bench._percentile([2.0], 0.95) == 2.0


def test_a_row_carries_the_size_of_the_table_it_was_measured_against(tmp_path: Path):
    proc_root = tmp_path / "proc"
    for pid in (1, 2, 3):
        (proc_root / str(pid)).mkdir(parents=True)
    (proc_root / "self").mkdir()

    assert bench.process_table_size(proc_root) == 3
    assert bench.process_table_size(tmp_path / "absent") == -1

    row = bench.time_case(bench.Case("noop", lambda: None, "does nothing"), repeat=3, warmup=1, proc_root=proc_root)

    # Without it a median cannot be compared with one from another machine, or
    # from the same machine on a busier day.
    assert row["processes_before"] == 3
    assert row["processes_after"] == 3
    assert row["repetitions"] == 3


def test_a_pass_is_reported_as_a_cadence_as_well_as_a_duration(monkeypatch):
    ticks = iter([0.0, 0.030] * 8)
    monkeypatch.setattr(bench.time, "thread_time", lambda: next(ticks))

    row = bench.time_case(bench.Case("fixed", lambda: None, "fixed cost"), repeat=4, warmup=0)

    assert row["cpu_ms"]["median"] == 30.0
    # 30 ms of a core every three seconds is one percent of it, which is the
    # arithmetic a reader would otherwise do by hand against a measured figure.
    assert row["implied_percent_of_core"]["every_3s"] == 1.0
    assert row["implied_percent_of_core"]["every_1s"] == 3.0


def test_a_primitive_that_refuses_is_a_result_rather_than_the_end_of_the_run():
    def explode() -> None:
        raise ValueError("no such Proton tool")

    row = bench.time_case(bench.Case("boom", explode, "refuses"), repeat=3, warmup=0)

    assert row["error"] == "ValueError: no such Proton tool"
    assert row["repetitions"] == 0
    assert "cpu_ms" not in row


def test_only_the_primitives_this_run_was_given_inputs_for_are_measured(tmp_path: Path):
    named = lambda cases: {case.name for case in cases}

    without = bench.build_cases(tmp_path, None, None, None, None)
    with_game = bench.build_cases(tmp_path, 620, "portal2.exe", 4321, tmp_path / "ce.exe")

    # Timing the table walk for an AppID that is not running measures its
    # cheapest state, so an absent input leaves the row out rather than
    # inventing one.
    assert named(without) == {"discover_proton_tools"}
    assert named(with_game) == {
        "discover_proton_tools", "observe_game_container", "game_target_state",
        "game_process_state", "inspect_ce_selection",
    }


def test_the_bench_refuses_a_host_that_has_no_process_table(monkeypatch, capsys, tmp_path: Path):
    from scripts import host_platform

    monkeypatch.setattr(
        bench.host_platform, "describe",
        lambda: host_platform.Host(
            system="Windows", family="windows", machine="AMD64", python_version="3.13.5",
            os_id=None, os_variant_id=None, os_version_id=None, os_build_id=None,
            steamos=False, vendor=None, product=None, device=None, procfs=False,
        ),
    )

    assert bench.main(["--home", str(tmp_path)]) == host_platform.EXIT_UNSUPPORTED_HOST
    assert "unsupported host" in capsys.readouterr().err


def test_an_out_of_range_repetition_count_is_refused_before_anything_runs(tmp_path: Path, capsys):
    assert bench.main(["--home", str(tmp_path), "--repeat", "0"]) == 2
    assert "--repeat" in capsys.readouterr().err
    assert bench.main(["--home", str(tmp_path / "absent")]) == 2
    assert "not a directory" in capsys.readouterr().err


class _FakeSocket:
    """Enough of the Decky client for the one RPC this helper makes."""

    def __init__(self, status: object) -> None:
        self.status = status

    def __enter__(self) -> "_FakeSocket":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def send_json(self, value: dict[str, object]) -> None:
        assert value["route"] == "loader/call_plugin_method"
        assert value["args"][1] == "get_status"


def _fake_backend(monkeypatch, status: object) -> None:
    import sys
    import types

    module = types.ModuleType("target_plugin_install")
    module.PLUGIN_NAME = "CE Decky"
    module._auth_token = lambda _url, _timeout: "token"
    module._await_reply = lambda _ws, _id: status
    module.DeckyWebSocket = types.SimpleNamespace(
        connect=lambda _url, _token, _timeout: _FakeSocket(status),
    )
    for name in ("target_plugin_install", "scripts.target_plugin_install"):
        monkeypatch.setitem(sys.modules, name, module)


def test_the_registered_cheat_engine_comes_from_the_backend_that_registered_it(monkeypatch, tmp_path: Path):
    executable = tmp_path / "cheatengine-x86_64.exe"
    executable.write_bytes(b"MZ")
    _fake_backend(monkeypatch, {"ce": {"executable": str(executable)}})

    resolved, reason = bench.registered_ce_executable("http://127.0.0.1:1337")

    # Reading the plugin's own configuration file from a developer helper would
    # be a second copy of a fact the live backend already reports.
    assert resolved == executable
    assert reason is None


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ({"ce": {"executable": None}}, "no Cheat Engine registered"),
        ({"ce": {}}, "no Cheat Engine registered"),
        ({}, "no Cheat Engine registered"),
        ({"ce": {"executable": "relative/cheatengine.exe"}}, "not absolute"),
        ({"ce": {"executable": "/absent/cheatengine.exe"}}, "is not a file"),
    ],
)
def test_an_answer_that_is_not_a_usable_executable_is_refused_with_its_reason(
    monkeypatch, status: object, expected: str,
):
    _fake_backend(monkeypatch, status)

    resolved, reason = bench.registered_ce_executable("http://127.0.0.1:1337")

    assert resolved is None
    assert expected in str(reason)


def test_a_backend_that_is_not_running_costs_one_row_rather_than_the_run(monkeypatch, tmp_path: Path):
    def refuse(*_args: object, **_kwargs: object) -> tuple[None, str]:
        return None, "ConnectionRefusedError: [Errno 111] Connection refused"

    monkeypatch.setattr(bench, "registered_ce_executable", refuse)

    report = bench.bench(tmp_path, None, None, None, None, repeat=1, warmup=0)

    assert report["ce_executable"] is None
    assert report["ce_executable_source"] == "unresolved"
    assert "Connection refused" in str(report["ce_executable_reason"])
    # Everything that did not need it still ran.
    assert [row["name"] for row in report["primitives"]] == ["discover_proton_tools"]


def test_an_explicitly_named_executable_is_not_second_guessed(monkeypatch, tmp_path: Path):
    def explode(*_args: object, **_kwargs: object) -> tuple[None, str]:
        raise AssertionError("the backend must not be asked when the path was given")

    monkeypatch.setattr(bench, "registered_ce_executable", explode)
    executable = tmp_path / "ce.exe"
    executable.write_bytes(b"MZ")

    report = bench.bench(tmp_path, None, None, None, executable, repeat=1, warmup=0)

    assert report["ce_executable"] == str(executable)
    assert report["ce_executable_source"] == "argument"


def test_the_identity_call_the_bench_makes_is_a_read():
    """The bench promises to write nothing and asks the backend for identity.

    That identity call used to label a Cheat Engine registered before this
    plugin read versions, which is a durable config write, so the default
    resolution path could mutate state. The label happens at load now. The
    property is proved against the service itself, in
    `tests/test_service.py::test_reading_the_status_of_a_legacy_registration_writes_nothing`;
    this holds the two together, so moving the write back cannot leave the
    bench quietly promising something it no longer does.
    """
    from ce_decky.service import PluginService
    import inspect

    source = inspect.getsource(PluginService.get_status)

    assert "_backfill_ce_version" not in source
    assert "config_store.save" not in source
