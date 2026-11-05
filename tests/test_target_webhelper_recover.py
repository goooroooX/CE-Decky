"""The webhelper recovery must end only what it proved wedged.

It signals processes on the device, so the cases that matter are the ones
where it would end the wrong thing: a thread busy in one sample only, a
helper thread spinning while its process's main thread waits, two browsers it
cannot tell apart, and a PID the kernel has since given to something else.
"""
from __future__ import annotations

from scripts import target_webhelper_recover as recover


def _process(pid, role, main_verdict, other_verdict="parked"):
    return {"pid": pid, "role": role, "threads": [
        {"tid": pid, "main": True, "verdict": main_verdict, "comm": "steamwebhelper"},
        {"tid": pid + 1, "main": False, "verdict": other_verdict, "comm": "Compositor"},
    ]}


def _sample(*processes):
    return {"processes": list(processes)}


def test_only_main_threads_spinning_in_both_samples_are_wedged():
    first = _sample(_process(10, "browser", "spinning"), _process(20, "renderer (inferred from its threads)", "spinning"),
                    _process(30, "renderer (inferred from its threads)", "spinning"))
    second = _sample(_process(10, "browser", "spinning"), _process(20, "renderer (inferred from its threads)", "spinning"),
                     _process(30, "renderer (inferred from its threads)", "parked"))
    assert recover.wedged(first, second) == ([20], 10)


def test_a_busy_helper_thread_under_a_waiting_main_thread_is_not_a_wedge():
    busy_compositor = _sample(_process(20, "renderer", "parked", other_verdict="spinning"))
    assert recover.wedged(busy_compositor, busy_compositor) == ([], None)


def test_two_spinning_browsers_are_not_guessed_between():
    both = _sample(_process(10, "browser", "spinning"), _process(11, "browser", "spinning"))
    assert recover.wedged(both, both) == ([], None)


def test_quick_access_counts_as_alive_only_when_its_javascript_runs():
    assert recover.quick_access_alive("QuickAccess_uid2: javascript running\nMainMenu_uid2: javascript running")
    assert not recover.quick_access_alive("QuickAccess_uid2: javascript unknown · could not attach: timed out")
    assert not recover.quick_access_alive("ui freeze probe: Steam CEF endpoint did not answer: timed out")


def test_a_reused_pid_reads_a_different_start_time(tmp_path):
    (tmp_path / "42").mkdir()
    fields = " ".join(str(n) for n in range(4, 30))
    (tmp_path / "42" / "stat").write_text(f"42 (steam webhelper) S {fields}")
    (tmp_path / "42" / "comm").write_text("steamwebhelper\n")
    # Field 22, counted after the command name in parentheses.
    assert recover.start_time(42, tmp_path) == "22"
    assert recover.is_webhelper(42, tmp_path)
    assert recover.start_time(43, tmp_path) is None and not recover.is_webhelper(43, tmp_path)
