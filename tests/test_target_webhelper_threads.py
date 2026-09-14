"""Reading a wedged Steam UI when CEF will not answer the debugger at all.

`target_ui_freeze_probe.py` is the better tool and answers the richer question,
but a renderer whose main thread is blocked below V8 does not answer the
debugger either: the probe reports that the whole renderer is blocked and then
has nothing further to give. That is the state a wedged Quick Access panel is
recovered from, and the recovery destroys the evidence.

These cases are about the parts of the fallback that can be wrong without
looking wrong: parsing `/proc` fields whose shape is decided by a process name
nobody controls, telling a spinning thread from a parked one, and not reporting
a renderer as a zygote just because Chromium never rewrote its command line.
"""
from __future__ import annotations

import pytest

from scripts import target_webhelper_threads as probe


def _sample(state: str = "S", wchan: str = "futex_do_wait") -> dict[str, object]:
    return {"tid": 1, "comm": "CrRendererMain", "state": state, "wchan": wchan, "ticks": 0}


def test_a_process_name_containing_spaces_and_brackets_is_parsed_correctly():
    # The `comm` field is whatever the process called itself, parentheses and
    # spaces included. Splitting on whitespace alone misreads every field after
    # it, which would silently report the wrong scheduler state for every
    # thread rather than failing.
    fields = probe._stat_fields("17 (steamw:sh (odd) name) R 1 17 17 0 -1 0 0 0 0 0 421 12")
    assert fields[2] == "R"
    assert fields[13] == "421"
    assert fields[14] == "12"


def test_a_stat_line_that_is_not_one_is_refused_rather_than_guessed_at():
    assert probe._stat_fields("") == []
    assert probe._stat_fields("nothing parenthesised here") == []


def test_a_thread_burning_processor_time_is_reported_as_spinning():
    # 100 ticks of 100 in one second is a thread that never yielded, which is
    # the signature of a loop rather than of a lock.
    assert probe._verdict(_sample(), ticks_delta=100, seconds=1.0, ticks=100.0) == "spinning"


def test_a_thread_parked_on_a_futex_is_not_called_blocked():
    # Chromium parks every idle worker on a futex, so calling this "blocked"
    # would report an ordinary idle browser as a deadlock and bury the one
    # thread that matters.
    assert probe._verdict(_sample(), ticks_delta=0, seconds=1.0, ticks=100.0) == "parked"


def test_an_epoll_wait_with_no_processor_time_is_idle():
    assert probe._verdict(
        _sample(wchan="do_epoll_wait"), ticks_delta=0, seconds=1.0, ticks=100.0,
    ) == "idle"
    # The kernel decorates some wait channels, so the match is on the prefix.
    assert probe._verdict(
        _sample(wchan="poll_schedule_timeout.constprop.0"), ticks_delta=0, seconds=1.0, ticks=100.0,
    ) == "idle"


def test_an_uninterruptible_thread_is_reported_as_itself():
    # A UI thread in D state is a storage or driver stall, not a loop, and it
    # outranks the processor-time reading because it cannot be signalled.
    assert probe._verdict(
        _sample(state="D", wchan="io_schedule"), ticks_delta=0, seconds=1.0, ticks=100.0,
    ) == "uninterruptible"


@pytest.mark.parametrize(
    ("declared", "threads", "expected"),
    [
        ("browser", {"CrBrowserMain"}, "browser"),
        ("utility", {"HangWatcher"}, "utility"),
        # A renderer forked from the zygote keeps the zygote's command line for
        # the whole of its life, so `--type=` calls every renderer a zygote.
        ("zygote", {"Compositor", "HangWatcher"}, "renderer (inferred from its threads)"),
        ("zygote", {"HangWatcher"}, "zygote or an idle fork of one"),
    ],
)
def test_a_renderer_is_named_from_its_threads_when_its_command_line_will_not_say(
    declared: str, threads: set[str], expected: str,
):
    assert probe._role(declared, threads) == expected


def test_an_unreadable_proc_file_is_an_empty_answer_rather_than_an_error(tmp_path):
    # Every read here races the process exiting, and a thread that ended between
    # the listing and the read is ordinary rather than a failure.
    assert probe._read(tmp_path / "definitely-absent") == ""


def test_the_probe_refuses_a_host_that_cannot_answer(monkeypatch, capsys):
    def _refuse(*_args, **_kwargs):
        raise probe.host_platform.UnsupportedHost("not the device")

    monkeypatch.setattr(probe.host_platform, "require", _refuse)
    monkeypatch.setattr("sys.argv", ["target_webhelper_threads.py"])

    # Exit code 3 is this project's "wrong machine", so a helper that cannot
    # answer says so by name instead of raising from the first POSIX-only call.
    assert probe.main() == 3
    assert "not the device" in capsys.readouterr().err
