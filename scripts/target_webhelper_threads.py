#!/usr/bin/env python3
"""Say what Steam's webhelper threads are doing when CEF will not answer.

`target_ui_freeze_probe.py` is the right tool first, and it answers the richer
question: it attaches to Steam's own debugging endpoint, proves whether a page
is still running JavaScript and prints the call stack that wedged it, by name
and source position. Use that one whenever it works.

This exists for the case where it cannot work. A renderer whose main thread is
blocked below V8 does not answer the debugger either, and the probe correctly
reports "the debugger itself did not answer; the whole renderer is blocked" and
then has nothing further to give. That is exactly the state a wedged Quick
Access panel is recovered from, and until now the only thing anybody could do
about it was restart the webhelper, which destroys the evidence.

So this reads the kernel's view instead, which needs no cooperation from the
process at all. For every `steamwebhelper` process and every thread in it, it
reports the thread's name, its scheduler state, the kernel function it is
sleeping in and the system call it is inside. It samples twice, because the one
discriminator that matters is not visible in a single sample:

  a thread burning processor time between two samples is spinning, and the
  defect is a loop;
  a thread using none, in `do_epoll_wait` or `poll`, is idle and healthy;
  a thread using none, parked on a futex, is waiting for something, and from
  outside the process there is no way to tell "waiting for work" from "waiting
  for a lock somebody else holds". Which thread it is settles that: Chromium
  parks every idle worker on a futex, so a parked worker is the normal state of
  an idle browser, and a parked `CrRendererMain` while the panel will not draw
  is the finding.

Chromium names its threads, so the report is readable without symbols:
`CrRendererMain` is the thread that runs the panel's JavaScript, `Compositor`
draws it, `Chrome_ChildIOThread` carries IPC, and `HangWatcher` is Chromium's
own hang detector. A `CrRendererMain` that is spinning and a `CrRendererMain`
that is blocked are different bugs with the same symptom.

Read-only, and unprivileged. It reads `/proc` and nothing else: it never
attaches a debugger, never stops a thread, never injects input, and changes
nothing about Steam, Decky or the running game. It is safe to run while the UI
is wedged, which is the only time it is worth running.

`--stacks` additionally asks `eu-stack` for native call stacks. On the Steam
Machine this works as the ordinary user for every webhelper process, renderer
included, which was not assumed: `kernel.yama.ptrace_scope=1` refuses `ptrace`
between unrelated processes, and the expectation was that it would refuse this.
It does not, so the option is worth reaching for. The frames come back as bare
addresses where Steam's own binaries carry no symbols, and `eu-stack` reports
one walk failure per thread whose unwind information runs out while still
returning the frames it did walk. Success is therefore judged on whether frames
came back rather than on whether anything was printed to stderr, and where none
did, the report gives the reason and the elevated command instead.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

if __package__:
    from . import host_platform
else:
    import host_platform

SCHEMA = 1
PROC = Path("/proc")
# Threads reported per process. A renderer runs a few dozen; a browser process
# with 48 is ordinary. Beyond this the report stops being readable and the
# threads that matter are named, so they are kept and the rest are counted.
MAX_THREADS = 64
# Thread names worth reporting even when the process is over the cap above.
# These are the ones a wedge is diagnosed from.
INTERESTING = (
    "CrRendererMain",
    "CrBrowserMain",
    "CrGpuMain",
    "Compositor",
    "HangWatcher",
    "Chrome_ChildIOThread",
    "ThreadPoolForegroundWorker",
)
# Kernel wait channels that mean "parked, waiting to be given something to do".
# A thread in one of these with no processor time is healthy, not stuck.
IDLE_WCHANS = (
    "do_epoll_wait", "do_sys_poll", "poll_schedule_timeout", "do_select",
    "pipe_read", "anon_pipe_read", "do_wait",
    "hrtimer_nanosleep", "do_nanosleep", "unix_stream_read_generic",
    "inet_csk_accept", "schedule_timeout",
)
# The scheduler states that mean the thread is on a processor or queued for one.
RUNNING_STATES = frozenset({"R"})
# Uninterruptible sleep. A thread here is inside the kernel and cannot be
# signalled; a UI thread in D state is a storage or driver stall, not a loop.
BLOCKED_STATES = frozenset({"D"})


def _clock_ticks() -> float:
    try:
        return float(os.sysconf("SC_CLK_TCK")) or 100.0
    except (ValueError, OSError, AttributeError):
        return 100.0


def _read(path: Path, limit: int = 4096) -> str:
    """Read one small procfs file, or return an empty string.

    Every read here races the process exiting, and a thread that ended between
    the listing and the read is an ordinary event rather than an error.
    """
    try:
        with path.open("rb") as handle:
            return handle.read(limit).decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _stat_fields(text: str) -> list[str]:
    """Split a `stat` line, tolerating a `comm` that contains spaces or `)`.

    The second field is the executable name in parentheses and is attacker- and
    accident-controlled; splitting on whitespace alone misreads every field
    after it. The last `)` is the only reliable boundary.
    """
    cut = text.rfind(")")
    if cut < 0:
        return []
    return ["", ""] + text[cut + 1:].split()


def _thread_sample(pid: int, tid: int) -> dict[str, object] | None:
    base = PROC / str(pid) / "task" / str(tid)
    stat = _stat_fields(_read(base / "stat"))
    if len(stat) < 15:
        return None
    try:
        utime, stime = int(stat[13]), int(stat[14])
    except (ValueError, IndexError):
        return None
    return {
        "tid": tid,
        "comm": _read(base / "comm", 64) or "?",
        "state": stat[2],
        "wchan": _read(base / "wchan", 128) or "0",
        # `syscall` is unreadable without ptrace permission on a hardened
        # kernel; its absence is not an error and the fields above still answer
        # the question.
        "syscall": (_read(base / "syscall", 256).split(" ") or ["?"])[0],
        "ticks": utime + stime,
    }


def _threads(pid: int) -> list[dict[str, object]]:
    try:
        tids = sorted(int(entry.name) for entry in (PROC / str(pid) / "task").iterdir())
    except (OSError, ValueError):
        return []
    samples = []
    for tid in tids:
        sample = _thread_sample(pid, tid)
        if sample is not None:
            samples.append(sample)
    return samples


def _process_type(pid: int) -> str:
    """Chromium's own `--type=`, which is how a renderer is told from a browser.

    Renderers on Linux are forked from the zygote and keep the zygote's command
    line, so a process reported as `zygote` with dozens of threads is in fact a
    renderer. That is a property of this build and not a misreading, so it is
    reported as it stands rather than guessed at.
    """
    try:
        raw = (PROC / str(pid) / "cmdline").read_bytes()
    except OSError:
        return "?"
    for part in raw.split(b"\0"):
        if part.startswith(b"--type="):
            return part.decode("utf-8", "replace")[len("--type="):] or "?"
    return "browser"


# Threads a renderer has and a zygote does not. A renderer forked from the
# zygote keeps the zygote's command line, so `--type=` calls it a zygote for the
# whole of its life; these names are what it actually is.
_RENDERER_THREADS = frozenset({"Compositor", "CompositorTileWorker", "Media"})


def _role(declared: str, thread_names: set[str]) -> str:
    """What this process is, where its own command line does not say.

    Chromium on Linux forks renderers from a zygote without rewriting `argv`, so
    every renderer reports `--type=zygote`. Reporting that alone sends a reader
    looking for a renderer that, by name, is not there. The command line is
    still reported as it stands under `type`; this is the reading of it, and it
    says when it is a reading.
    """
    if declared != "zygote":
        return declared
    if thread_names & _RENDERER_THREADS:
        return "renderer (inferred from its threads)"
    return "zygote or an idle fork of one"


def _webhelper_pids() -> list[int]:
    found = []
    for entry in PROC.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if _read(entry / "comm", 64) == "steamwebhelper":
            found.append(pid)
    return sorted(found)


def _verdict(sample: dict[str, object], ticks_delta: int, seconds: float, ticks: float) -> str:
    """What this thread is doing, in the terms the question is asked in.

    `parked` deserves its wording. Chromium parks every idle thread-pool worker
    on a futex, so a futex wait is overwhelmingly "waiting to be given work" and
    only occasionally "waiting for a lock somebody else holds". Nothing outside
    the process can tell those apart, so this does not pretend to: it says the
    thread is parked and leaves the distinction to which thread it is. A parked
    `ThreadPoolForegroundWorker` is the normal state of an idle browser; a
    parked `CrRendererMain` while the panel will not draw is the finding.
    """
    state = str(sample.get("state", "?"))
    wchan = str(sample.get("wchan", "0"))
    cpu_share = (ticks_delta / ticks) / seconds if seconds > 0 else 0.0
    if cpu_share >= 0.5:
        return "spinning"
    if state in BLOCKED_STATES:
        return "uninterruptible"
    if state in RUNNING_STATES:
        return "running"
    if cpu_share > 0.02:
        return "working"
    if wchan.startswith(IDLE_WCHANS):
        return "idle"
    if wchan.startswith("futex"):
        return "parked"
    return "sleeping"


def _native_stacks(pid: int, timeout: float) -> dict[str, object]:
    """Native frames for one process, or the reason there are none.

    Judged on whether frames came back, not on what `stderr` said. Those are
    different questions on this device: `eu-stack` prints one
    "Callback returned failure" per thread whose unwind information runs out and
    still returns the frames it did walk, so keying success off that text threw
    away real answers. A refusal, by contrast, produces headers and nothing else.

    Frames come back as bare addresses when a library has no symbols, which is
    the ordinary case for Steam's own binaries. That is still an answer: the
    thread names and the wait channels above say what is stuck, and `stderr`
    names the library each walk stopped in.
    """
    binary = shutil.which("eu-stack")
    if binary is None:
        return {"ok": False, "reason": "eu-stack is not installed on this host"}
    try:
        completed = subprocess.run(
            [binary, "-p", str(pid)], capture_output=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": f"eu-stack did not finish within {timeout:g}s"}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"[:200]}
    out = completed.stdout.decode("utf-8", "replace")
    err = completed.stderr.decode("utf-8", "replace")
    # A frame line begins with its frame number; the `PID`/`TID` headers are
    # printed even when every thread was refused, so they are not an answer.
    frames = [line for line in out.splitlines() if line.lstrip().startswith("#")]
    if frames:
        return {
            "ok": True,
            # True when some threads produced frames and others did not, so a
            # reader knows the walk is incomplete rather than the whole picture.
            "partial": bool(err.strip()),
            "frames": out.splitlines()[:400],
            "stderr": err.splitlines()[:20],
        }
    scope = _read(Path("/proc/sys/kernel/yama/ptrace_scope"), 8) or "?"
    detail = next((line for line in err.splitlines() if line.strip()), "")
    return {
        "ok": False,
        "reason": (
            f"eu-stack walked no frames (kernel.yama.ptrace_scope={scope}):"
            f" {detail[:160] or 'no reason given'}"
        ),
        "elevated_command": f"sudo eu-stack -p {pid}",
    }


def _collect(interval: float, stacks: bool, stack_timeout: float, verbose: bool) -> dict[str, object]:
    ticks = _clock_ticks()
    pids = _webhelper_pids()
    first = {pid: {int(t["tid"]): t for t in _threads(pid)} for pid in pids}
    started = time.monotonic()
    time.sleep(interval)
    elapsed = time.monotonic() - started
    processes = []
    for pid in pids:
        second = _threads(pid)
        if not second:
            continue
        rows = []
        for sample in second:
            tid = int(sample["tid"])
            before = first.get(pid, {}).get(tid)
            delta = int(sample["ticks"]) - int(before["ticks"]) if before else 0
            rows.append({
                "tid": tid,
                "comm": sample["comm"],
                "state": sample["state"],
                "wchan": sample["wchan"],
                "syscall": sample["syscall"],
                "cpu_ticks": delta,
                "cpu_share": round((delta / ticks) / elapsed, 3) if elapsed > 0 else 0.0,
                "verdict": _verdict(sample, delta, elapsed, ticks),
            })
        # Busiest first, then the named threads, then the rest: what wedged the
        # UI is either burning a processor or sitting in a lock with a name.
        rows.sort(key=lambda row: (-row["cpu_ticks"], str(row["comm"])))
        # The thread whose id equals the process id is the one that ran `main`,
        # which in a renderer is the thread that runs the panel's JavaScript.
        # Chromium does not always rename it, so it is identified by id rather
        # than by looking for a name that may not be there.
        for row in rows:
            row["main"] = row["tid"] == pid
        named = [row for row in rows if row["main"] or str(row["comm"]) in INTERESTING]
        rest = [row for row in rows if not (row["main"] or str(row["comm"]) in INTERESTING)]
        if not verbose:
            # Everything that is doing something, plus the named threads. An
            # idle pool worker is not why the UI stopped, and forty of them
            # push the thread that is off the screen.
            rest = [row for row in rest if row["verdict"] not in {"idle", "parked", "sleeping"}]
        kept = (named + rest)[:MAX_THREADS]
        declared = _process_type(pid)
        entry: dict[str, object] = {
            "pid": pid,
            "type": declared,
            "role": _role(declared, {str(row["comm"]) for row in rows}),
            "threads": len(rows),
            "omitted_threads": max(0, len(rows) - len(kept)),
            "busy_threads": sum(1 for row in rows if row["verdict"] in {"spinning", "running", "working"}),
            "rows": kept,
        }
        if stacks:
            entry["native_stacks"] = _native_stacks(pid, stack_timeout)
        processes.append(entry)
    return {
        "schema": SCHEMA,
        "sampled_over_seconds": round(elapsed, 3),
        "processes": processes,
        "webhelper_processes": len(pids),
    }


def _render(report: dict[str, object]) -> str:
    lines = [
        f"steamwebhelper processes: {report['webhelper_processes']}"
        f"   sampled over {report['sampled_over_seconds']}s",
        "* marks the process main thread. 'parked' is a futex wait: normal for a"
        " worker, a finding for a main thread that will not draw.",
        "",
    ]
    for process in report.get("processes", []):
        lines.append(
            f"pid {process['pid']}  {process['role']}  threads={process['threads']}"
            f"  busy={process['busy_threads']}"
        )
        for row in process.get("rows", []):
            lines.append(
                f"  {'*' if row.get('main') else ' '} {row['tid']:<8} {str(row['comm'])[:22]:<22}"
                f" {row['state']}  {row['verdict']:<9} cpu={row['cpu_share']:<6} {str(row['wchan'])[:28]}"
            )
        if process.get("omitted_threads"):
            lines.append(
                f"    ... {process['omitted_threads']} further threads idle or parked"
                " (--all-threads lists them)"
            )
        native = process.get("native_stacks")
        if isinstance(native, dict):
            if native.get("ok"):
                lines.append("    native stack:")
                lines.extend(f"      {frame}" for frame in list(native.get("frames", []))[:40])
            else:
                lines.append(f"    native stack unavailable: {native.get('reason')}")
                if native.get("elevated_command"):
                    lines.append(f"      try: {native['elevated_command']}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="What Steam's webhelper threads are doing, read from /proc rather than from CEF.",
    )
    parser.add_argument(
        "--interval", type=float, default=1.0,
        help="seconds between the two samples; longer separates a slow loop from a busy one (default 1.0)",
    )
    parser.add_argument(
        "--stacks", action="store_true",
        help="also ask eu-stack for native call stacks; needs ptrace permission this user usually lacks",
    )
    parser.add_argument("--stack-timeout", type=float, default=30.0, help="seconds one eu-stack call may take")
    parser.add_argument(
        "--all-threads", action="store_true",
        help="list every thread, not only the named ones and the ones doing something",
    )
    parser.add_argument("--json", action="store_true", help="print the whole report as JSON")
    parser.add_argument("--out", type=Path, default=None, help="also write the JSON report to this path")
    args = parser.parse_args()

    try:
        host_platform.require("The webhelper thread probe")
    except host_platform.UnsupportedHost as exc:
        print(str(exc), file=sys.stderr)
        return 3

    if args.interval <= 0 or args.interval > 60:
        print("--interval must be between 0 and 60 seconds", file=sys.stderr)
        return 2

    report = _collect(args.interval, args.stacks, args.stack_timeout, args.all_threads)
    if not report["processes"]:
        print("No steamwebhelper process is running on this device.", file=sys.stderr)
        return 1
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else _render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
