"""Every helper either runs on this host or says why it cannot, before it works.

The same checkout is developed from Windows and from the device. Two failures
are equally expensive and this file is against both: a helper that starts on the
wrong machine and dies at its first POSIX-only call, leaving an agent to work
out from a traceback that the task belongs elsewhere; and a helper with no
platform requirement that grows a guard it does not need and stops running where
it always did.
"""
from __future__ import annotations

import ast
from pathlib import Path
import re

import pytest

from scripts import host_platform

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

#: Surfaces that do not exist, or do not mean the same thing, off the target.
POSIX_ONLY = re.compile(r"/proc/|\"/proc|os\.sysconf|os\.getuid|os\.geteuid|os\.killpg|os\.uname|signal\.SIGKILL")

#: Helpers that touch one of those surfaces and answer for the host some other
#: way. Each entry is a reason, not a waiver: a new name here has to be one.
EXEMPT = {
    "host_platform.py": "it is the module that answers the question",
    "browser_harness.py": "current_pin refuses a non-Linux host by name before any download",
    "browser_probe.py": "it is only ever started by browser_harness, which has already refused the host",
}


def _scripts() -> list[Path]:
    return sorted(path for path in SCRIPTS.glob("*.py") if not path.name.startswith("_"))


#: Calls that do not exist on Windows at all. At module scope one of these
#: fails on the import line, where nothing can report an unsupported host: what
#: an agent reads is an AttributeError. This is exactly how the session cost
#: probe used to fail.
POSIX_ONLY_CALLS = {"sysconf", "getuid", "geteuid", "uname", "killpg", "fork", "setsid"}


def _module_level_posix_calls(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    functions = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
    found: list[str] = []

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, functions):
                continue
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr in POSIX_ONLY_CALLS
            ):
                found.append(f"{child.func.attr} at line {child.lineno}")
            walk(child)

    walk(tree)
    return found


def test_no_helper_makes_a_posix_only_call_where_a_refusal_cannot_reach_it():
    offenders = {path.name: calls for path in _scripts() if (calls := _module_level_posix_calls(path))}

    assert not offenders, (
        "these calls run at import, before any helper can say which machine this is:"
        f" {offenders}"
    )


#: What each guarded helper requires, as a decision on record rather than
#: whatever `require()` happens to default to. Adding a helper means adding a
#: row here, which is the point: the host class is a choice about what the
#: helper reads, and choosing it silently is how a device-only helper ends up
#: accepting a desktop.
#:
#: `steamos` is required only where a helper reads something that exists only on
#: the device's own session. Everything else is Linux-generic on purpose: the
#: cost probe and the primitive bench compare two machines, so refusing a
#: non-SteamOS Linux host would refuse half of what they are for, and the state
#: and prefix probes work against exact paths a maintainer may hold anywhere.
#: Whether this machine *is* the device is a separate question, and
#: `target_agent_preflight.py` is what answers it.
REQUIREMENTS = {
    "target_archive_probe.py": {"needs_procfs": False},
    "target_ce_launch_probe.py": {},
    "target_decky_env_probe.py": {},
    "target_memory_watch.py": {},
    "target_plugin_log.py": {"needs_procfs": False},
    "target_plugin_install.py": {},
    # Talks to Decky over its own socket and reads no process table of its
    # own; what it needs is the device and a live Decky on it.
    "target_plugin_rpc.py": {"needs_procfs": False},
    "target_prefix_probe.py": {"needs_procfs": False},
    "target_primitive_bench.py": {},
    "target_screenshot.py": {"needs_valve_hardware": True},
    "target_session_cost_probe.py": {},
    "target_state_probe.py": {},
    "target_ui_freeze_probe.py": {"needs_procfs": False},
    # Reads every webhelper thread out of `/proc`, so the process table is the
    # whole of what it needs and the default requirement is the correct one.
    "target_webhelper_threads.py": {},
    "target_ui_layout_probe.py": {"needs_procfs": False},
    # Asks Steam's own JavaScript what the library holds, through the same CEF
    # endpoint as the two above, so a process table is not what it needs either.
    "target_steam_library_probe.py": {"needs_procfs": False},
    # Reads what the plugin is drawing through that same endpoint.
    "target_panel_read.py": {"needs_procfs": False},
}


def _declared_requirement(source: str) -> dict[str, bool]:
    call = source[source.index("host_platform.require(") :]
    call = call[: call.index(")\n")]
    declared: dict[str, bool] = {}
    for flag in ("needs_procfs", "needs_steamos", "needs_valve_hardware"):
        match = re.search(rf"{flag}\s*=\s*(True|False)", call)
        if match is not None:
            declared[flag] = match.group(1) == "True"
    return declared


def test_each_guarded_helper_requires_the_host_class_it_was_given():
    """The guard is only as good as what it asks for.

    A helper that calls `require()` with the defaults accepts any Linux host
    with a process table, which is right for some of them and wrong for one
    that reads the device's own session. This holds each choice against the
    table above so a new helper cannot inherit an answer nobody made.
    """
    guarded = {
        path.name: _declared_requirement(path.read_text(encoding="utf-8"))
        for path in _scripts()
        if "host_platform.require(" in path.read_text(encoding="utf-8")
    }

    assert guarded == REQUIREMENTS


def test_a_helper_that_needs_the_target_refuses_a_host_that_is_not_one():
    missing: list[str] = []
    for path in _scripts():
        source = path.read_text(encoding="utf-8")
        if not POSIX_ONLY.search(source) or path.name in EXEMPT:
            continue
        if "host_platform.require(" not in source:
            missing.append(path.name)
    assert not missing, (
        "these helpers read something only the target has and would fail at that call"
        f" instead of refusing the host: {missing}"
    )


def test_the_exemptions_are_still_helpers_and_still_answer_for_the_host():
    for name, reason in EXEMPT.items():
        path = SCRIPTS / name
        assert path.is_file(), f"exemption {name} names a helper that is gone: {reason}"


def test_a_helper_with_no_platform_requirement_is_not_given_one():
    """Repository tooling runs on Windows, and a guard here would break that."""
    unrestricted = ("qa.py", "verify_repo.py", "package_plugin.py", "changelog.py", "check_release.py",
                    "frontend_source_digest.py", "table_ui_probe.py", "target_tls_probe.py")
    for name in unrestricted:
        source = (SCRIPTS / name).read_text(encoding="utf-8")
        assert "host_platform.require(" not in source, f"{name} has no platform requirement to enforce"


def test_the_guard_runs_after_argument_parsing_so_help_still_answers_anywhere():
    """`--help` is how an agent finds out what a helper is for.

    Refusing before argparse would make the wrong machine unable even to read
    that, which is the opposite of the point.
    """
    for path in _scripts():
        source = path.read_text(encoding="utf-8")
        if "host_platform.require(" not in source or path.name in EXEMPT:
            continue
        guard = source.index("host_platform.require(")
        if "parse_args(" not in source:
            # A helper that takes no arguments has nothing to parse first.
            continue
        assert "parse_args(" in source[:guard], (
            f"{path.name} refuses the host before it has parsed its arguments"
        )


def test_the_refusal_names_the_helper_and_the_machine():
    host = host_platform.Host(
        system="Windows", family="windows", machine="AMD64", python_version="3.13.5",
        os_id=None, os_variant_id=None, os_version_id=None, os_build_id=None,
        steamos=False, vendor=None, product=None, device=None, procfs=False,
    )
    with pytest.raises(host_platform.UnsupportedHost) as refusal:
        host_platform.require("The example probe", host=host)

    message = str(refusal.value)
    assert message.startswith("The example probe")
    assert "Windows on AMD64" in message
