"""Run the real resident bridge under a stock Lua interpreter.

The shipped `ce_decky_bridge.lua` used to be provable only on target, which put
descriptor handling, the control protocol, startup application, result
reporting and status rendering behind T7/T8. This module executes the exact
production script against a Cheat Engine API stub, feeds it descriptors and
control files produced by the production Python renderers, and validates the
status it writes with the production Python parser.

What this does NOT prove: real Cheat Engine semantics. MemoryRecord activation,
Auto Assembler/Lua execution, `md5file`, process attachment, timer behaviour and
the exact Lua build inside Cheat Engine remain target evidence (T7/T8).
"""
from __future__ import annotations

from hashlib import md5, sha256
from pathlib import Path
import os
import shutil
import subprocess

import pytest

from ce_decky.session_protocol import (
    MAX_COMMANDS,
    MAX_STARTUP_ACTIONS,
    RuntimeCommand,
    SessionDescriptor,
    StartupAction,
    parse_descriptor,
    parse_status,
    percent_encode,
    render_control,
    render_descriptor,
)

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "py_modules" / "ce_decky" / "ce_decky_bridge.lua"
STUB = ROOT / "tests" / "lua" / "ce_stub.lua"
RUNNER = ROOT / "tests" / "lua" / "run_bridge.lua"
SESSION_ID = "123e4567-e89b-42d3-a456-426614174000"
CE_SHA = "a" * 64
TABLE_SHA = "b" * 64
INTERPRETERS = ("lua", "lua5.4", "lua54", "lua5.3", "lua53", "luajit")


def _interpreter() -> str:
    for name in INTERPRETERS:
        found = shutil.which(name)
        if found:
            return found
    pytest.skip("no Lua interpreter is available; install lua5.4 to run bridge conformance")


def _lua_string(value: str) -> str:
    """Encode one Lua literal.

    JSON escapes are not Lua escapes: Lua has no `\\uXXXX`, so non-ASCII bytes
    are emitted as decimal escapes of their exact UTF-8 encoding.
    """
    out = ['"']
    for byte in value.encode("utf-8"):
        if byte == 0x22 or byte == 0x5C:
            out.append(chr(92) + chr(byte))
        elif 0x20 <= byte < 0x7F:
            out.append(chr(byte))
        else:
            out.append(chr(92) + f"{byte:03d}")
    out.append('"')
    return "".join(out)


def _lua_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "nil"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return _lua_string(value)
    if isinstance(value, dict):
        items = ", ".join(f"[{_lua_value(key)}] = {_lua_value(item)}" for key, item in value.items())
        return "{ " + items + " }"
    if isinstance(value, (list, tuple)):
        return "{ " + ", ".join(_lua_value(item) for item in value) + " }"
    raise TypeError(f"unsupported scenario value: {value!r}")


class BridgeRun:
    def __init__(self, root: Path, stdout: str, stderr: str) -> None:
        self.root = root
        self.stdout = stdout
        self.stderr = stderr

    @property
    def diagnostics(self) -> list[str]:
        return [line.split("\t", 1)[1] for line in self.stdout.splitlines() if line.startswith("#OUT#\t")]

    @property
    def loaded(self) -> bool:
        return any(line == "#LOADED#\ttrue" for line in self.stdout.splitlines())

    @property
    def status_bytes(self) -> bytes | None:
        status = self.root / "status.txt"
        return status.read_bytes() if status.is_file() else None

    def status(self):
        raw = self.status_bytes
        assert raw is not None, f"bridge wrote no status: {self.diagnostics} {self.stderr}"
        return parse_status(raw)


def _descriptor(
    *,
    target_process: str = "game.exe",
    startup: tuple[StartupAction, ...] = (),
    table_path: str = "Z:\\table.ct",
    control_path: str = "Z:\\control.txt",
    status_path: str = "Z:\\status.txt",
    table_has_lua: bool = False,
    switch_off: tuple[tuple[int, str], ...] = (),
) -> bytes:
    return render_descriptor(
        SessionDescriptor(
            session_id=SESSION_ID,
            app_id=220,
            ce_sha256=CE_SHA,
            table_sha256=TABLE_SHA,
            table_path=table_path,
            target_process=target_process,
            control_path=control_path,
            status_path=status_path,
            startup=startup,
            is_shortcut=False,
            table_has_lua=table_has_lua,
            switch_off=switch_off,
        )
    )


def _run(
    tmp_path: Path,
    *,
    descriptor: bytes,
    scenario: dict[str, object],
    controls: dict[str, bytes] | None = None,
    descriptor_md5: str | None = None,
    descriptor_sha256: str | None = None,
) -> BridgeRun:
    interpreter = _interpreter()
    root = tmp_path / "session"
    root.mkdir(parents=True, exist_ok=True)
    (root / "descriptor.txt").write_bytes(descriptor)
    (root / "table.ct").write_bytes(b"<CheatTable/>\n")
    for name, payload in (controls or {}).items():
        (root / name).write_bytes(payload)
    if not (root / "control.txt").is_file():
        (root / "control.txt").write_bytes(render_control([]))

    scenario = {"root": root.as_posix(), **scenario}
    scenario.setdefault("md5_default", descriptor_md5 or md5(descriptor).hexdigest())
    scenario_path = tmp_path / "scenario.lua"
    scenario_path.write_text("return " + _lua_value(scenario) + "\n", encoding="utf-8")

    environment = dict(os.environ)
    environment.update({
        "CE_DECKY_DESCRIPTOR": "Z:\\descriptor.txt",
        "CE_DECKY_DESCRIPTOR_SHA256": descriptor_sha256 or sha256(descriptor).hexdigest(),
        "CE_DECKY_DESCRIPTOR_MD5": descriptor_md5 or md5(descriptor).hexdigest(),
    })
    completed = subprocess.run(
        [interpreter, str(RUNNER), str(STUB), str(scenario_path), str(BRIDGE)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(ROOT),
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return BridgeRun(root, completed.stdout, completed.stderr)


def _control(*commands: RuntimeCommand) -> bytes:
    return render_control(commands)


# -- CE window lifecycle ---------------------------------------------------


def test_bridge_preserves_ce_onshow_but_suppresses_the_first_window_map(tmp_path: Path):
    descriptor = _descriptor()
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={"target_process": "game.exe", "steps": [{"ticks": 1}]},
    )
    assert run.loaded
    assert "#MAINFORM#\t1\t2\tfalse" in run.stdout
    assert "#APPLICATION#\tfalse" in run.stdout


# -- refused activation ------------------------------------------------------


def test_bridge_separates_a_refused_activation_from_an_undifferentiated_failure(tmp_path: Path):
    # Cheat Engine accepted the change, finished, and the record is still off.
    # For a script record that is Cheat Engine declining to run it, which is what
    # a table whose patterns no longer match the game's build does - and it is
    # the one mutation failure a user can act on.
    descriptor = _descriptor()
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "records": {5: {"active": False, "activation_never_settles": True}},
            "steps": [{"ticks": 4}],
        },
        controls={"control.txt": _control(RuntimeCommand(1, "set_active", 5, "1"))},
    )
    status = run.status()
    refused = [item for item in status.results if item.record_id == 5]
    assert refused and refused[-1].ok is False
    assert refused[-1].error_code == "activation_rejected"
    assert refused[-1].error == "activation did not settle"


# -- table loading ----------------------------------------------------------


def test_bridge_loads_the_session_table_itself_and_reports_that_it_did(tmp_path: Path):
    # Cheat Engine opens a table named on its command line only once its main
    # window is shown, and CE Decky never lets that window map, so the launch
    # does not name one. Without this the game got a Cheat Engine attached to it
    # with an empty address list: every record missing, nothing to switch on and
    # nothing to pin.
    descriptor = _descriptor()
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "load_table_records": {1: {"active": False}},
            "steps": [{"ticks": 2}],
        },
    )
    status = run.status()
    assert status.table_load_state == "loaded"
    assert status.table_load_error is None
    assert status.address_list_count == 1


def test_bridge_authorizes_the_tables_own_lua_script_instead_of_letting_ce_ask(tmp_path: Path):
    # Cheat Engine asks before it runs a table's own Lua script, and the question
    # is one of its own modal forms: it stops the bridge's bootstrap where it
    # stands, the window sweep will not close an enumerated form, and over a
    # running game nobody can see it to answer it. Measured on the device: this
    # is what held Half-Life 2 for the whole five-minute launch budget with no
    # heartbeat at all. The user authorized this exact table SHA for execution in
    # Review before Cheat Engine was started, so the stream overload carries that
    # answer into the load and the question is never raised.
    descriptor = _descriptor()
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "load_table_records": {1: {"active": False}},
            "steps": [{"ticks": 2}],
        },
    )
    status = run.status()
    assert status.table_load_state == "loaded"
    assert status.table_load_route == "approved"
    # One stream, opened read-only and shared, the table loaded from it with the
    # Lua-script question suppressed, and the handle released afterwards.
    assert "#LOADTABLE#\t1\tstream\ttrue\t1\t64\t1" in run.stdout


def test_bridge_publishes_a_heartbeat_before_it_opens_the_table(tmp_path: Path):
    # Opening a table is the one bootstrap step that runs code Cheat Engine and
    # the table brought with them, so it is the one that can stop for as long as
    # it likes. Everything that makes a stopped bootstrap visible used to start
    # only once the bootstrap had finished, which is why five minutes of it read
    # as "the bridge never reported a heartbeat" rather than as where it stopped.
    descriptor = _descriptor()
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "load_table_records": {1: {"active": False}},
            "steps": [{"ticks": 2}],
        },
    )
    assert "#LOADORDER#\ttrue" in run.stdout
    # And the phase is honest about what that early heartbeat means: by the end
    # of the run the bootstrap has finished.
    assert run.status().bridge_phase == "ready"


def test_bridge_falls_back_to_the_path_form_and_says_cheat_engine_may_ask(tmp_path: Path):
    # CE Decky can be pointed at a Cheat Engine the user imported themselves,
    # and an older one exposes no stream to load a table from. It must still be
    # able to open one; what changes is that Cheat Engine is free to ask about
    # the table's Lua script, so the route is published rather than assumed.
    descriptor = _descriptor()
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "no_file_stream": True,
            "load_table_records": {1: {"active": False}},
            "steps": [{"ticks": 2}],
        },
    )
    status = run.status()
    assert status.table_load_state == "loaded"
    assert status.table_load_route == "prompted"
    assert "#LOADTABLE#\t1\tpath\tfalse\t0\tnil\t0" in run.stdout


def test_bridge_falls_back_when_this_cheat_engine_refuses_a_stream(tmp_path: Path):
    # A build that has the stream constructor but not the overload that takes
    # one. Losing the table entirely would be a worse answer than a load that
    # Cheat Engine may ask about.
    descriptor = _descriptor()
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "no_stream_overload": True,
            "load_table_records": {1: {"active": False}},
            "steps": [{"ticks": 2}],
        },
    )
    status = run.status()
    assert status.table_load_state == "loaded"
    assert status.table_load_route == "prompted"


def test_bridge_refuses_a_lua_table_it_would_have_to_be_asked_about(tmp_path: Path):
    # The path form is the only fallback for a Cheat Engine the user imported
    # themselves, and for a table that carries a Lua script it is not a risk but
    # a certainty: Cheat Engine raises a modal form from inside the load, the
    # window sweep hides it because hiding is what it does to Cheat Engine's own
    # forms, and nothing on the device can answer it. Left to run, the launch
    # waits out its whole 300 second budget and then says nothing was heard.
    # Failing in a second, with a reason, is the answer the user can act on.
    descriptor = _descriptor(table_has_lua=True)
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "no_file_stream": True,
            "load_table_records": {1: {"active": False}},
            "steps": [{"ticks": 4}],
        },
    )
    status = run.status()
    assert status.table_load_state == "failed"
    assert status.table_load_route == "prompted"
    assert "run its Lua script" in (status.table_load_error or "")
    # And it is refused rather than attempted: nothing was handed to Cheat
    # Engine at all.
    assert "#LOADTABLE#\t0\t" in run.stdout


def test_the_refusal_reports_what_the_approved_route_actually_said(tmp_path: Path):
    # The refusal above is only true of a Cheat Engine that offers no such route
    # at all. One that offered it and refused has said something of its own, and
    # that is the finding: asserting "this Cheat Engine has to ask" for a build
    # that never asked anything would record a cause nobody established and
    # advise the Cheat Engine the user is already running.
    descriptor = _descriptor(table_has_lua=True)
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "no_stream_overload": True,
            "load_table_records": {1: {"active": False}},
            "steps": [{"ticks": 4}],
        },
    )
    status = run.status()
    assert status.table_load_state == "failed"
    assert status.table_load_route == "prompted"
    error = status.table_load_error or ""
    assert "loadTable does not accept a stream" in error
    assert "use the Cheat Engine CE Decky installs" not in error
    # The stream it did create is still released.
    assert "#LOADTABLE#\t1\tstream\ttrue\t1\t64\t1" in run.stdout


def test_bridge_still_opens_a_script_free_table_on_a_cheat_engine_without_streams(tmp_path: Path):
    # The refusal above is scoped to the question Cheat Engine would actually
    # ask. A table with no Lua script raises none, so an imported Cheat Engine
    # keeps working exactly as it did.
    descriptor = _descriptor(table_has_lua=False)
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "no_file_stream": True,
            "load_table_records": {1: {"active": False}},
            "steps": [{"ticks": 2}],
        },
    )
    status = run.status()
    assert status.table_load_state == "loaded"
    assert status.table_load_route == "prompted"


def test_bridge_releases_the_stream_even_when_the_load_raises(tmp_path: Path):
    # The load reads the whole stream before it returns, so the handle is spent
    # either way, and a load that raises is exactly the case that retries: eight
    # attempts releasing nothing would hold eight handles on the session table
    # for the life of this Cheat Engine.
    descriptor = _descriptor()
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "no_stream_overload": True,
            "load_table_records": {1: {"active": False}},
            "steps": [{"ticks": 2}],
        },
    )
    assert run.status().table_load_route == "prompted"
    # Two loads for the one attempt, the stream form that raised and the path
    # form behind it; one stream created, and released despite the raise.
    assert "#LOADTABLE#\t2\tpath\tfalse\t1\t64\t1" in run.stdout


def test_bridge_says_so_when_this_cheat_engine_cannot_open_a_table_at_all(tmp_path: Path):
    # CE Decky can be pointed at a Cheat Engine the user imported themselves. One
    # that does not expose `loadTable` cannot be driven, and saying so once beats
    # eight retries ending in "attempt to call a nil value".
    descriptor = _descriptor()
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={"target_process": "game.exe", "no_load_table": True, "steps": [{"ticks": 4}]},
    )
    status = run.status()
    assert status.table_load_state == "failed"
    assert "does not expose loadTable" in (status.table_load_error or "")


def test_bridge_reports_a_table_cheat_engine_refused_to_open(tmp_path: Path):
    # An address list that stayed empty is not a table CE Decky cannot support;
    # it is a load that failed, and saying so is what separates the two.
    descriptor = _descriptor()
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "load_table_refuses": True,
            # Eight attempts, spaced four ticks apart, plus the one at startup.
            "steps": [{"ticks": 40}],
        },
    )
    status = run.status()
    assert status.table_load_state == "failed"
    assert status.table_load_error == "Cheat Engine refused to open the table"
    assert status.address_list_count == 0


def test_bridge_reports_a_table_load_that_raised(tmp_path: Path):
    descriptor = _descriptor()
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "load_table_error": True,
            "steps": [{"ticks": 40}],
        },
    )
    status = run.status()
    assert status.table_load_state == "failed"
    assert "loadTable failed" in (status.table_load_error or "")


def test_bridge_hides_a_cheat_engine_window_mapped_after_the_first_show(tmp_path: Path):
    # Suppression is installed from the main form's first show, which is when CE
    # maps the window that takes a running game's audio and controller input. A
    # window CE maps later left an empty frame over the game with the gamepad
    # overlay drawing a focus box around it, and nothing put it back down.
    descriptor = _descriptor()
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "extra_forms": 1,
            "steps": [{"ticks": 2}, {"show_window": 1, "ticks": 8}],
        },
    )
    status = run.status()
    assert status.window_suppressions is not None and status.window_suppressions >= 1
    # `#MAINFORM#` is show-calls, hide-calls, and whether the form is still up.
    mainform = next(line for line in run.stdout.splitlines() if line.startswith("#MAINFORM#"))
    shows, hides, visible = mainform.split("\t")[1:4]
    assert shows == "1" and visible == "false"
    # One hide for the first show, at least one more for the window CE mapped
    # afterwards.
    assert int(hides) >= 2


def test_bridge_takes_down_a_form_the_tables_own_lua_script_created(tmp_path: Path):
    # A table's Lua script now runs, so a table can create a form of its own,
    # and Cheat Engine's `hideAllCEWindows` does not reach one. Measured on this
    # device: a 300x120 trainer form reported Visible true both before and after
    # that helper, stayed mapped over Half-Life 2 for the whole session, and
    # went down the moment `Visible` was assigned false. The sweep enumerates
    # that form already, so it hides what it can itself see.
    descriptor = _descriptor(table_has_lua=True)
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "extra_forms": 1,
            "script_forms": {1: True},
            "steps": [{"ticks": 2}, {"show_window": 1, "ticks": 8}],
        },
    )
    status = run.status()
    assert status.window_suppressions is not None and status.window_suppressions >= 1
    # And the sweep is not reporting a success it did not have.
    assert status.unsuppressed_sweeps in (None, 0)
    assert status.window_over_game in (None, False)


def test_bridge_counts_a_window_it_could_not_put_down_as_exactly_that(tmp_path: Path):
    # The counter used to mean "a window was seen and the hide did not raise",
    # which for a form Cheat Engine's own helper cannot reach was neither: on the
    # device it climbed once a second for the whole session while the window it
    # was counting never moved. A window that stays up whatever this does is the
    # worst outcome the sweep has, and it must be the one thing it cannot be
    # mistaken for.
    descriptor = _descriptor(table_has_lua=True)
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "extra_forms": 1,
            "script_forms": {1: True},
            # A form that refuses the assignment as well, which is a window
            # nothing here can take off the game.
            "unhidable_script_forms": True,
            "steps": [{"ticks": 2}, {"show_window": 1, "ticks": 8}],
        },
    )
    status = run.status()
    assert status.window_suppressions == 0
    # The sweep runs on a timer, so this is a count of attempts and one window
    # raises it once per tick. It is published as what it is, and the panel is
    # told separately that a window is on the game right now.
    assert status.unsuppressed_sweeps is not None and status.unsuppressed_sweeps >= 1
    assert status.window_over_game is True


def test_a_window_that_goes_down_stops_being_reported_as_over_the_game(tmp_path: Path):
    # The count only ever climbs, so it cannot say the screen came back: a
    # window the table closed itself left the panel telling the user the game
    # was still covered for the rest of the session.
    descriptor = _descriptor(table_has_lua=True)
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "extra_forms": 1,
            "script_forms": {1: True},
            "unhidable_script_forms": True,
            "steps": [
                {"ticks": 2},
                {"show_window": 1, "ticks": 8},
                # The window stops refusing, and the next sweep takes it down.
                {"unhidable_script_forms": False, "ticks": 4},
            ],
        },
    )
    status = run.status()
    # What it cost to get the screen back is still on the record.
    assert status.unsuppressed_sweeps is not None and status.unsuppressed_sweeps >= 1
    assert status.window_over_game is False
    assert status.window_suppressions is not None and status.window_suppressions >= 1


def test_bridge_dismisses_a_window_the_form_sweep_cannot_reach(tmp_path: Path):
    # A message dialog is not a form assigned to the application, so the sweep
    # reported everything hidden and its counter stayed at zero while a 360x108
    # "Confirmation" sat over the game and took the screen from it. A window
    # this process owns and holds in the foreground is on screen whatever it is
    # made of; hiding cannot reach it, so it is closed and said out loud.
    descriptor = _descriptor()
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "steps": [
                {"ticks": 2},
                {"foreground_window": {"handle": 4096, "caption": "Confirmation"}, "ticks": 12},
            ],
        },
    )
    status = run.status()
    assert status.dialogs_dismissed == 1
    assert status.last_dialog == "Confirmation"
    sent = next(line for line in run.stdout.splitlines() if line.startswith("#SENTMESSAGES#"))
    assert sent.split("\t")[1] == "1"


# The game's windows as the device reports them: the caption the window list
# gives, the handle `findWindow` resolves it to, and the process that handle
# really belongs to. "Default IME" resolving to another process is not invented.
def _sent_messages(run) -> list[dict[str, int]]:
    """Every message the bridge sent or posted, as the stub recorded it."""
    messages = []
    for line in run.stdout.splitlines():
        if line.startswith("#MESSAGE#"):
            _, handle, message, wparam, posted = line.split("\t")[:5]
            messages.append({
                "handle": int(handle), "message": int(message),
                "wparam": int(wparam), "posted": posted == "true",
            })
    return messages


GAME_WINDOWS = [["Default IME", 197592, 776], ["GAME - Vulkan", 65716, 4321]]


@pytest.mark.parametrize("shape", ["captions", "pairs"])
def test_bridge_asks_the_game_to_come_back_from_minimized(tmp_path: Path, shape: str):
    # A game that loses the foreground to a Cheat Engine window minimizes
    # itself, and taking that window away does not undo it: on the device the
    # game kept its sound and its controller and drew nothing at all, and
    # mapping and activating the window from outside changed nothing. This is
    # the request that did work. The device answers the window list with bare
    # captions and the manual describes {id, caption} pairs, so entries are read
    # both ways.
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "window_list_shape": shape,
            "main_form_handle": 1024,
            "steps": [{
                "ticks": 12,
                "game_windows": GAME_WINDOWS,
                # The game minimized itself when Cheat Engine took the screen,
                # which is what Windows is asked about before anything is sent.
                "iconic_windows": [65716],
                # Wine leaves the foreground on the window Cheat Engine hid.
                "foreground_window": {"handle": 1024, "caption": "Cheat Engine 7.7"},
            }],
        },
    )

    status = run.status()
    assert status.game_restores is not None and status.game_restores >= 1
    restores = [
        message for message in _sent_messages(run)
        if message["message"] == 0x0112 and message["wparam"] == 0xF120
    ]
    assert restores, "the game was never asked to restore"
    # Posted, not sent. `SendMessage` does not return until the receiving
    # thread handles the message, and the receiving thread is a game that may
    # be loading or not pumping at all, which would stop this bridge's timer
    # with it - no heartbeat, no runtime commands, for as long as it took.
    assert all(message["posted"] for message in restores)
    # Only the window the attached process actually owns: a caption is not an
    # identity, and the other one belongs to a different process entirely.
    assert {message["handle"] for message in restores} == {65716}
    # Once, not once a sweep. A game that came back maximized would be sent to
    # windowed size by the second ask, and Cheat Engine keeps the foreground on
    # a window it has already hidden, so the gate alone would not stop it.
    assert len(restores) == 1


def test_bridge_leaves_a_game_alone_while_it_holds_the_foreground(tmp_path: Path):
    # A game in front is not a minimized game, and Windows says so.
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "steps": [{
                "ticks": 12,
                "game_windows": GAME_WINDOWS,
                "foreground_window": {"handle": 65716, "caption": "GAME - Vulkan", "pid": 4321},
            }],
        },
    )

    assert run.status().game_restores is None
    assert not [
        message for message in _sent_messages(run)
        if message["message"] == 0x0112 and message["wparam"] == 0xF120
    ]


def test_bridge_asks_again_only_until_the_game_is_back_in_front(tmp_path: Path):
    # The ask stops on its own: the game takes the foreground when it restores,
    # and from then on there is nothing to undo.
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "main_form_handle": 1024,
            "steps": [
                {
                    "ticks": 4,
                    "game_windows": GAME_WINDOWS,
                    "iconic_windows": [65716],
                    "foreground_window": {"handle": 1024, "caption": "Cheat Engine 7.7"},
                },
                {"foreground_window": {"handle": 65716, "caption": "GAME - Vulkan", "pid": 4321}, "ticks": 20},
            ],
        },
    )

    restores = [
        message for message in _sent_messages(run)
        if message["message"] == 0x0112 and message["wparam"] == 0xF120
    ]
    # Asked while the game was away, and not once after it came back.
    assert restores
    assert len(restores) <= 2


def test_bridge_never_restores_a_game_that_is_not_minimized(tmp_path: Path):
    # The regression this exists to prevent. A game that stays up when it loses
    # the foreground is behind Cheat Engine's window and perfectly healthy, and
    # `SC_RESTORE` sent to a maximized window means "back to windowed size" - so
    # asking it anything would drop a working game out of fullscreen. Being
    # behind something is not being minimized, and only Windows can tell them
    # apart.
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "main_form_handle": 1024,
            "steps": [{
                "ticks": 12,
                "game_windows": GAME_WINDOWS,
                # Cheat Engine has the screen and the game is still maximized.
                "iconic_windows": [],
                "foreground_window": {"handle": 1024, "caption": "Cheat Engine 7.7"},
            }],
        },
    )

    assert run.status().game_restores is None
    assert run.status().minimized_query == "IsIconic"
    assert run.status().restore_capability == "ready"
    assert run.status().restore_error is None
    assert not [
        message for message in _sent_messages(run)
        if message["message"] == 0x0112 and message["wparam"] == 0xF120
    ]


def test_bridge_asks_nothing_when_minimized_state_cannot_be_established(tmp_path: Path):
    # No proof, no request. A Cheat Engine that cannot be asked whether a window
    # is minimized leaves the game exactly as it is, and says so, rather than
    # sending something that is only safe for half the answers.
    # `minimized_query` is `unavailable` for all three, which is the whole
    # reason the capability is reported separately: these are different faults
    # with different fixes and that field cannot tell them apart.
    for scenario, capability in (
        ({"no_execute_code_local": True}, "no-local-call"),
        ({"execute_code_local_error": True}, "iconic-unanswered"),
        # A call that answers something other than what the caller thinks it
        # does: the main form is not minimized, so an answer that says it is
        # disqualifies the whole mechanism.
        ({"lying_iconic": True}, "iconic-disagrees"),
    ):
        run = _run(
            tmp_path,
            descriptor=_descriptor(),
            scenario={
                "target_process": "game.exe",
                "target_pid": 4321,
                "main_form_handle": 1024,
                **scenario,
                "steps": [{
                    "ticks": 12,
                    "game_windows": GAME_WINDOWS,
                    "iconic_windows": [65716],
                    "foreground_window": {"handle": 1024, "caption": "Cheat Engine 7.7"},
                }],
            },
        )
        assert run.status().game_restores is None, scenario
        assert run.status().minimized_query == "unavailable", scenario
        assert run.status().restore_capability == capability, scenario
        assert not [
            message for message in _sent_messages(run)
            if message["message"] == 0x0112 and message["wparam"] == 0xF120
        ], scenario


@pytest.mark.parametrize("variant", ["unqualified_iconic_only", "dirty_return_register"])
def test_bridge_reads_the_minimized_answer_the_way_the_call_returns_it(tmp_path: Path, variant: str):
    # The unqualified export is tried when the module-qualified name is not
    # there, and a 32-bit BOOL arrives in a register whose upper half is not
    # part of the answer - read whole, an unminimized game would look minimized.
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "main_form_handle": 1024,
            variant: True,
            "steps": [{
                "ticks": 12,
                "game_windows": GAME_WINDOWS,
                "iconic_windows": [65716],
                "foreground_window": {"handle": 1024, "caption": "Cheat Engine 7.7"},
            }],
        },
    )
    restores = [
        message for message in _sent_messages(run)
        if message["message"] == 0x0112 and message["wparam"] == 0xF120
    ]
    assert {message["handle"] for message in restores} == {65716}


def test_bridge_finds_a_game_window_that_no_caption_resolves_to(tmp_path: Path):
    # A caption is not unique either. `findWindow` answers with the first window
    # in the system wearing it, and on the device the game's own "Default IME"
    # caption resolved to a window belonging to another process entirely -
    # after which the game's real window by that name could never be reached.
    # Walking the system's own top-level chain and keeping what the exact
    # attached process owns needs no caption at all.
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "main_form_handle": 1024,
            "steps": [{
                "ticks": 12,
                # Every caption the game reports resolves to a foreign window.
                "game_windows": [["Default IME", 197592, 776]],
                # The game's actual window, which only enumeration finds.
                "system_windows": [[65716, 4321]],
                "iconic_windows": [65716],
                "foreground_window": {"handle": 1024, "caption": "Cheat Engine 7.7"},
            }],
        },
    )

    restores = [
        message for message in _sent_messages(run)
        if message["message"] == 0x0112 and message["wparam"] == 0xF120
    ]
    assert {message["handle"] for message in restores} == {65716}


def test_bridge_never_closes_a_window_the_hide_sweep_reaches(tmp_path: Path):
    # Hiding is what deals with a form, and anything the hide sweep reaches is
    # never closed. Wine leaves the foreground on a window that was hidden
    # rather than destroyed, so a Cheat Engine tool window or a form an
    # authorized table created for itself can be down and still be the handle
    # in front - and closing it would cancel whatever it was doing.
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        scenario={
            "target_process": "game.exe",
            "extra_forms": 2,
            "steps": [
                {"ticks": 2},
                # `Handle` 2048 + index is what the stub gives form 1.
                {"foreground_window": {"handle": 2049, "caption": "CE Form 1"}, "ticks": 12},
            ],
        },
    )
    status = run.status()
    assert status.dialogs_dismissed is None
    sent = next(line for line in run.stdout.splitlines() if line.startswith("#SENTMESSAGES#"))
    assert sent.split("\t")[1] == "0"


def test_bridge_closes_nothing_while_the_forms_cannot_be_identified(tmp_path: Path):
    # A build whose forms answer to neither `Handle` nor `Caption` leaves the
    # sweep unable to prove the window in front is not one of them, and a window
    # that cannot be identified is not one to answer on the user's behalf.
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        scenario={
            "target_process": "game.exe",
            "extra_forms": 1,
            "forms_without_identity": True,
            "steps": [
                {"ticks": 2},
                {"foreground_window": {"handle": 4096, "caption": "Confirmation"}, "ticks": 12},
            ],
        },
    )
    assert run.status().dialogs_dismissed is None


def test_bridge_waits_for_the_proof_before_it_uses_the_minimized_answer(tmp_path: Path):
    # Reading the main form's handle is what materializes it, and a form that
    # has none yet is not a build that cannot answer - it is one that has not
    # been asked yet. Until the known-state window has actually proven the
    # mechanism, the capability does not exist and nothing is sent to anything,
    # however minimized the game is.
    def restores_for(steps):
        run = _run(
            tmp_path,
            descriptor=_descriptor(),
            scenario={
                "target_process": "game.exe",
                "target_pid": 4321,
                "main_form_handle": 1024,
                # The handle only appears after the first window sweep.
                "main_form_handle_after_ticks": 6,
                "steps": steps,
            },
        )
        return run, [
            message for message in _sent_messages(run)
            if message["message"] == 0x0112 and message["wparam"] == 0xF120
        ]

    # Minimized while the mechanism is unproven, and back by the time it is
    # proven: nothing may have been sent in between.
    run, restores = restores_for([
        {"ticks": 5, "game_windows": GAME_WINDOWS, "iconic_windows": [65716]},
        {"ticks": 16, "iconic_windows": []},
    ])
    assert restores == []
    # Still proven afterwards, so this is the proof arriving late rather than
    # a Cheat Engine that cannot answer at all.
    assert run.status().minimized_query == "IsIconic"
    assert run.status().restore_capability == "ready"

    # Still minimized when the proof lands: asked exactly once, and only then.
    run, restores = restores_for([
        {"ticks": 5, "game_windows": GAME_WINDOWS, "iconic_windows": [65716]},
        {"ticks": 16},
    ])
    assert len(restores) == 1
    assert restores[0]["handle"] == 65716


def test_bridge_resolves_through_the_lookup_the_target_actually_answers(tmp_path: Path):
    """The device's exact condition: one symbol table lookup, not the other.

    Cheat Engine has two entry points into its own symbol table and they reach
    different resolvers. On the target `getAddressSafe(name, true)` answers
    nothing for `user32` at all, which is why nothing could be asked back, and
    it says nothing about `getAddress(name, true)` - the one Cheat Engine's own
    scripts rely on when they call a `user32` predicate by name.

    Here the first is blind and the second answers, which is the shape the
    device reported. A bridge that reached for the blind one would find an
    empty table and send nothing.
    """
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "main_form_handle": 1024,
            "safe_lookup_blind": True,
            "steps": [{"ticks": 12, "game_windows": GAME_WINDOWS, "iconic_windows": [65716]}],
        },
    )
    status = run.status()
    assert status.restore_capability == "ready"
    assert status.minimized_query == "IsIconic"
    assert status.restore_error is None
    restores = [
        message for message in _sent_messages(run)
        if message["message"] == 0x0112 and message["wparam"] == 0xF120
    ]
    # Exactly one, and only to the window that is actually iconic.
    assert len(restores) == 1
    assert restores[0]["handle"] == 65716


def test_bridge_reads_the_calls_out_of_the_module_when_the_symbol_table_is_blind(tmp_path: Path):
    """The device's actual condition, measured: the symbol table answers nothing.

    A Cheat Engine started inside the game's own Wine session resolves nothing
    through its own symbol table, for anything, permanently: not `user32`, not
    even Cheat Engine's own executable. The same Cheat Engine, table and Proton
    resolve everything in a prefix of its own, so this is a property of running
    where the game runs, which is where CE Decky always runs it.

    The module list beside that table is complete and correct, so the two calls
    are read out of `user32.dll`'s own loaded image instead. Nothing is called
    by name either way, and the answer is still proven against the hidden main
    form before any game window is asked anything.
    """
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "main_form_handle": 1024,
            "symbol_table_blind": True,
            "steps": [{"ticks": 12, "game_windows": GAME_WINDOWS, "iconic_windows": [65716]}],
        },
    )
    status = run.status()
    assert status.restore_capability == "ready"
    assert status.minimized_query == "IsIconic"
    assert status.restore_error is None
    restores = [
        message for message in _sent_messages(run)
        if message["message"] == 0x0112 and message["wparam"] == 0xF120
    ]
    assert len(restores) == 1
    assert restores[0]["handle"] == 65716


@pytest.mark.parametrize(
    "scenario",
    [
        # The file header does not admit to carrying a data directory at all.
        {"image_optional_header_size": 0x60},
        # It carries none.
        {"image_directory_count": 0},
        # The export directory is shorter than the ten fields that are read
        # out of it.
        {"image_export_size": 8},
        # The directory, or one of the three tables inside it, starts inside
        # the image and does not end inside it.
        {"image_export_rva": 0xFFF0},
        {"image_names_rva": 0xFFFF},
        {"image_ordinals_rva": 0xFFFF},
        {"image_functions_rva": 0xFFFF},
        # A table that is simply not there.
        {"image_names_rva": 0},
    ],
)
def test_bridge_refuses_an_image_whose_own_headers_do_not_cover_it(tmp_path: Path, scenario: dict):
    """Every address read here comes out of the image's own headers.

    This is Cheat Engine's own memory, so an address that leaves the image does
    not fail: it returns some other module's bytes, and what would be called
    afterwards is whatever those bytes were taken to mean. So each table is
    proven to lie wholly inside the image before it is indexed, and the header
    fields the format provides for bounding a data-directory read are required
    rather than assumed.
    """
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "main_form_handle": 1024,
            "symbol_table_blind": True,
            **scenario,
            "steps": [{"ticks": 96, "game_windows": GAME_WINDOWS, "iconic_windows": [65716]}],
        },
    )
    assert run.status().restore_capability == "iconic-unanswered", scenario
    assert run.status().minimized_query == "unavailable", scenario
    assert not [m for m in _sent_messages(run) if m["message"] in (0x0112, 0x001C)], scenario


def test_bridge_waits_for_a_module_list_that_is_not_built_yet_before_attaching(tmp_path: Path):
    """Attaching is what ends the only chance to read Cheat Engine's own modules.

    `enumModules()` describes the process Cheat Engine has open, so once the
    game is attached it describes the game. An empty list before that is Cheat
    Engine not having built its own yet, and crossing the attach boundary on it
    would lose the one route to the two calls for the whole session, on a device
    where the symbol table answers nothing.
    """
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "main_form_handle": 1024,
            "symbol_table_blind": True,
            # Empty for the first few ticks, which is inside the bounded wait.
            "self_module_list_empty_until_tick": 4,
            "steps": [
                {"ticks": 12, "game_windows": GAME_WINDOWS, "iconic_windows": [65716]},
                {"ticks": 8, "iconic_windows": []},
            ],
        },
    )
    status = run.status()
    assert status.restore_capability == "ready"
    assert status.minimized_query == "IsIconic"
    # The wait is not a refusal to work: the game is still attached afterwards.
    assert status.attached is True
    assert status.game_restores == 1
    assert status.game_activations == 1


def test_bridge_does_not_load_the_table_while_the_module_list_is_still_empty(tmp_path: Path):
    """Loading the table is itself something that can open a process.

    Cheat Engine runs the table's own hooks and its Lua when it opens one, and
    either may attach to a process. That ends the only moment `enumModules()`
    describes Cheat Engine rather than the game, so holding only the bridge's
    own attach back is not enough: with the first module list still empty, the
    table load would cross that boundary before the retry could read it, and on
    a Cheat Engine whose symbol table answers nothing the two calls that bring a
    game back would be unreachable for the rest of the session.
    """
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "main_form_handle": 1024,
            "symbol_table_blind": True,
            "self_module_list_empty_until_tick": 4,
            "load_table_opens_process": True,
            "load_table_records": {1: {"active": False}},
            "steps": [
                {"ticks": 16, "game_windows": GAME_WINDOWS, "iconic_windows": [65716]},
                {"ticks": 8, "iconic_windows": []},
            ],
        },
    )
    status = run.status()
    # The table still loads, and the image fallback survived the table doing
    # exactly what the bridge warns it can do.
    assert status.table_load_state == "loaded"
    assert status.restore_capability == "ready"
    assert status.minimized_query == "IsIconic"
    assert status.game_restores == 1
    assert status.game_activations == 1


def test_bridge_attaches_anyway_when_the_module_list_never_arrives(tmp_path: Path):
    # The wait is bounded. A Cheat Engine whose module list never appears must
    # still attach and run the table; only the ability to ask a game back is
    # lost, and it says so.
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "main_form_handle": 1024,
            "symbol_table_blind": True,
            "self_module_list_empty_until_tick": 10_000,
            "steps": [{"ticks": 96, "game_windows": GAME_WINDOWS, "iconic_windows": [65716]}],
        },
    )
    status = run.status()
    assert status.attached is True
    assert status.restore_capability == "iconic-unanswered"
    assert not [m for m in _sent_messages(run) if m["message"] == 0x0112]


def test_bridge_does_not_wait_on_a_module_list_that_answered(tmp_path: Path):
    # A list longer than the walk, or one without the module in it, is an
    # answer. Waiting on it would hold the attach back for nothing, and it must
    # not be cached as "the module is not there" either.
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "main_form_handle": 1024,
            "symbol_table_blind": True,
            "self_module_list_oversized": True,
            "steps": [{"ticks": 96, "game_windows": GAME_WINDOWS, "iconic_windows": [65716]}],
        },
    )
    status = run.status()
    assert status.attached is True
    assert status.restore_capability == "iconic-unanswered"
    assert not [m for m in _sent_messages(run) if m["message"] == 0x0112]


def test_bridge_resolves_the_post_from_the_image_without_a_symbol_lookup(tmp_path: Path):
    # `getAddress` missing is not the same as the address being unavailable.
    # A Cheat Engine that can read the export out of the module resolves both
    # halves that way, and refusing the second one on the absence of a lookup
    # the first one did not need would leave the game dark for no reason.
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "main_form_handle": 1024,
            "no_get_address": True,
            "no_get_address_safe": True,
            "steps": [
                {"ticks": 8, "game_windows": GAME_WINDOWS, "iconic_windows": [65716]},
                {"ticks": 8, "iconic_windows": []},
            ],
        },
    )
    status = run.status()
    assert status.restore_capability == "ready"
    assert status.minimized_query == "IsIconic"
    assert status.game_restores == 1
    assert status.game_activations == 1


def test_bridge_says_why_the_post_half_could_not_be_resolved(tmp_path: Path):
    # The question half succeeding clears the last refusal, so without keeping
    # this the status would name a stopping point and say nothing about it.
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "main_form_handle": 1024,
            "no_post_message": True,
            "no_self_module_list": True,
            "steps": [{"ticks": 96, "game_windows": GAME_WINDOWS, "iconic_windows": [65716]}],
        },
    )
    status = run.status()
    assert status.restore_capability == "no-post"
    assert "PostMessageW" in (status.restore_error or ""), status.restore_error


def test_bridge_never_reads_the_module_list_of_the_attached_game(tmp_path: Path):
    """`enumModules()` answers for whatever Cheat Engine has open.

    It is Cheat Engine's own list only while nothing is attached. Read after
    attaching it would name the game's modules at the game's addresses, and an
    address from the game's address space, called inside Cheat Engine, is the
    exact mistake every other line of this file exists to avoid. So the list is
    read once, before the game is attached, and a Cheat Engine that had nothing
    to read then simply has no answer.
    """
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "main_form_handle": 1024,
            "symbol_table_blind": True,
            # No main form window at the moment the bridge loads, so discovery
            # cannot finish there and every later attempt runs while attached.
            "main_form_handle_after_ticks": 8,
            "steps": [{"ticks": 24, "game_windows": GAME_WINDOWS, "iconic_windows": [65716]}],
        },
    )
    # The module list was still read before the attach, so the capability is
    # established from the base taken then, and never from the game's.
    assert run.status().restore_capability == "ready"
    restores = [
        message for message in _sent_messages(run)
        if message["message"] == 0x0112 and message["wparam"] == 0xF120
    ]
    assert [message["handle"] for message in restores] == [65716]


def test_bridge_tells_a_restored_game_that_it_is_active_again(tmp_path: Path):
    """Coming back from minimized is not the same as being active again.

    Cheat Engine takes the foreground as it starts, the game is deactivated and
    minimizes itself, and CE Decky then hides every Cheat Engine window, which
    leaves the session with no foreground window at all. The restore puts the
    window back with nothing to reactivate it, so the game goes on running the
    loop it uses while it is not the active application. Measured on the device:
    Half-Life 2 came back on screen at 18 frames a second instead of 60, stayed
    there for the whole session, and stayed there after Cheat Engine exited.
    Posting `WM_ACTIVATEAPP` to the window that was asked back put it to 60.

    It is sent to a window this session actually restored, and only once that
    window reports itself no longer minimized, so the game has handled the
    restore before it arrives.
    """
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "main_form_handle": 1024,
            "steps": [
                {"ticks": 8, "game_windows": GAME_WINDOWS, "iconic_windows": [65716]},
                # The game answered the restore, exactly as the device does.
                {"ticks": 8, "iconic_windows": []},
            ],
        },
    )
    posts = _sent_messages(run)
    restores = [m for m in posts if m["message"] == 0x0112 and m["wparam"] == 0xF120]
    activations = [m for m in posts if m["message"] == 0x001C]
    assert [m["handle"] for m in restores] == [65716]
    assert [m["handle"] for m in activations] == [65716], "the restored window was never told it is active"
    assert activations[0]["wparam"] == 1
    assert run.status().game_activations == 1
    # The restore is posted first: the activation is the answer to a window that
    # has already come back.
    assert posts.index(restores[0]) < posts.index(activations[0])


def test_bridge_activates_a_window_restored_by_the_last_sweep_of_its_budget(tmp_path: Path):
    """The two halves cannot share one budget.

    The restore is posted in one sweep and the window reports itself back in a
    later one, so a window restored by the last sweep of the restore budget had
    no sweep left to be told it is active in, and would have come back on screen
    and stayed in the inactive state the activation exists to leave.
    """
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "main_form_handle": 1024,
            # Nothing to ask back for the first five sweeps, so the restore
            # lands on the last one the budget has.
            "steps": [
                {"ticks": 20, "game_windows": GAME_WINDOWS, "iconic_windows": []},
                {"ticks": 8, "iconic_windows": [65716]},
                {"ticks": 8, "iconic_windows": []},
            ],
        },
    )
    posts = _sent_messages(run)
    assert [m["handle"] for m in posts if m["message"] == 0x0112 and m["wparam"] == 0xF120] == [65716]
    assert [m["handle"] for m in posts if m["message"] == 0x001C] == [65716]
    assert run.status().game_activations == 1


def test_bridge_activates_a_window_that_takes_its_time_coming_back(tmp_path: Path):
    """The restore is posted, not sent, and a loading game answers when it can.

    Waiting for the window to come back and trying to tell it that it is active
    are two different waits. Spending the activation's own attempts while the
    window is still minimized threw them away before there was anything to send,
    so a game that took longer than that to handle the restore came back on
    screen and stayed in the slow inactive state.
    """
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "main_form_handle": 1024,
            "steps": [
                # Well past the handful of attempts the activation itself gets.
                {"ticks": 120, "game_windows": GAME_WINDOWS, "iconic_windows": [65716]},
                {"ticks": 8, "iconic_windows": []},
            ],
        },
    )
    posts = _sent_messages(run)
    assert [m["handle"] for m in posts if m["message"] == 0x0112 and m["wparam"] == 0xF120] == [65716]
    assert [m["handle"] for m in posts if m["message"] == 0x001C] == [65716]
    assert run.status().game_activations == 1


def test_bridge_gives_up_on_activating_a_window_that_never_comes_back(tmp_path: Path):
    # A window that stays minimized, or that is destroyed and recreated under
    # another handle, must not keep the sweep asking for the rest of the
    # session. The pair of counters is what says which half was missed.
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "main_form_handle": 1024,
            # Past the sweeps a restore is given to land in, so the give-up
            # path itself runs rather than the test ending inside the wait.
            "steps": [{"ticks": 280, "game_windows": GAME_WINDOWS, "iconic_windows": [65716]}],
        },
    )
    status = run.status()
    assert status.game_restores == 1
    assert status.game_activations is None
    # Exactly one ask, not one per sweep for the rest of the session.
    assert len([
        m for m in _sent_messages(run) if m["message"] == 0x0112 and m["wparam"] == 0xF120
    ]) == 1


def test_bridge_never_activates_a_window_it_did_not_ask_back(tmp_path: Path):
    # Telling a game it is active is only ever the second half of asking it
    # back. A game that was never pushed aside is left alone entirely.
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "main_form_handle": 1024,
            "steps": [{"ticks": 16, "game_windows": GAME_WINDOWS, "iconic_windows": []}],
        },
    )
    assert not [m for m in _sent_messages(run) if m["message"] == 0x001C]
    assert run.status().game_activations is None
    assert run.status().game_restores is None


def test_bridge_finds_the_game_windows_with_nothing_in_the_foreground(tmp_path: Path):
    """There is no foreground window in a session CE Decky runs.

    The game's windows are found by walking the system's own top-level chain,
    and a chain has to be entered from a window. That seed was the foreground
    window, and CE Decky keeps every Cheat Engine window hidden: on the device
    `getForegroundWindow` answered zero for the whole session, so the chain was
    never walked, no window was ever found to belong to the game, and a game
    that could have been asked back never was. Cheat Engine's own main form is
    a top-level window whether or not it is visible, and that is the seed.
    """
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "main_form_handle": 1024,
            "steps": [{
                "ticks": 12,
                "game_windows": GAME_WINDOWS,
                "iconic_windows": [65716],
                # Nothing of Cheat Engine's is in front, which is the state the
                # window suppression exists to keep it in.
                "foreground_window": False,
            }],
        },
    )
    assert run.status().restore_capability == "ready"
    restores = [
        message for message in _sent_messages(run)
        if message["message"] == 0x0112 and message["wparam"] == 0xF120
    ]
    assert [message["handle"] for message in restores] == [65716]


def test_bridge_runs_once_when_cheat_engine_loads_it_twice(tmp_path: Path):
    """The private runtime really does load it twice, and it must not run twice.

    `main.lua` loads the bridge so it is running before Cheat Engine enumerates
    its own autorun directory, and the bridge file lives in that directory, so
    Cheat Engine loads it again a moment later. On the device both copies were
    live in one Lua state, writing the same status file over each other. The
    second copy starts after the game has been attached, which is exactly when
    Cheat Engine's module list stops being its own, so it could establish
    nothing and it was the one writing last: a session that could ask the game
    back reported that it could not.
    """
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "main_form_handle": 1024,
            "symbol_table_blind": True,
            "load_bridge_twice": True,
            "steps": [{"ticks": 12, "game_windows": GAME_WINDOWS, "iconic_windows": [65716]}],
        },
    )
    status = run.status()
    assert status.restore_capability == "ready"
    assert status.minimized_query == "IsIconic"
    restores = [
        message for message in _sent_messages(run)
        if message["message"] == 0x0112 and message["wparam"] == 0xF120
    ]
    # One bridge, so one ask, not one per copy.
    assert len(restores) == 1


def test_bridge_asks_the_game_back_when_the_symbol_table_answers_late(tmp_path: Path):
    """The device's exact condition: the table is built while Cheat Engine starts.

    The bridge is loaded from `main.lua`, before Cheat Engine has enumerated its
    own autorun directory, and the first thing it asks for is a `user32` export.
    On the target every name came back unresolved at that moment - and a probe
    run of the same Cheat Engine, the same table and the same Proton resolved
    all of them a moment later. The first answer is therefore not the session's
    answer, and treating it as one is what left the game dark for the whole
    session: nothing was ever asked again.

    Here nothing resolves for the first eight ticks. The capability has to
    become `ready` by itself afterwards, and the game still has to be asked back
    once it does, because the minimize happened while the table was empty.
    """
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "main_form_handle": 1024,
            "symbols_blind_until_tick": 8,
            "no_self_module_list": True,
            "steps": [{"ticks": 24, "game_windows": GAME_WINDOWS, "iconic_windows": [65716]}],
        },
    )
    status = run.status()
    assert status.restore_capability == "ready"
    assert status.minimized_query == "IsIconic"
    # The reading that failed belonged to that moment, not to the session.
    assert status.restore_error is None
    restores = [
        message for message in _sent_messages(run)
        if message["message"] == 0x0112 and message["wparam"] == 0xF120
    ]
    assert len(restores) == 1
    assert restores[0]["handle"] == 65716


def test_bridge_says_the_capability_is_still_settling_while_it_waits(tmp_path: Path):
    # While the table has nothing in it the panel has to say so rather than
    # reporting a refusal, because this state becomes `ready` on its own and a
    # refusal is what sends someone to file a bug about a working Cheat Engine.
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "main_form_handle": 1024,
            "symbols_blind_until_tick": 200,
            "no_self_module_list": True,
            "steps": [{"ticks": 12, "game_windows": GAME_WINDOWS, "iconic_windows": [65716]}],
        },
    )
    assert run.status().restore_capability == "symbols-unresolved"
    assert run.status().minimized_query is None
    assert not [message for message in _sent_messages(run) if message["message"] == 0x0112]


def test_bridge_reloads_the_symbol_table_once_when_it_answers_nothing(tmp_path: Path):
    # The self handler can be asked before it has anything to answer with. One
    # reload corrects that; repeating it would reload symbols every sweep for
    # the rest of the session.
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "main_form_handle": 1024,
            "symbols_need_reload": True,
            "no_self_module_list": True,
            "steps": [{"ticks": 40, "game_windows": GAME_WINDOWS, "iconic_windows": [65716]}],
        },
    )
    assert run.status().restore_capability == "ready"
    assert len([
        message for message in _sent_messages(run)
        if message["message"] == 0x0112 and message["wparam"] == 0xF120
    ]) == 1


def test_bridge_gives_up_when_a_reload_does_not_help(tmp_path: Path):
    # Nothing resolves even after the reload, so the refusal stands and names
    # every spelling tried rather than being retried forever.
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "main_form_handle": 1024,
            "no_get_address": True,
            "no_self_module_list": True,
            "steps": [{"ticks": 96, "game_windows": GAME_WINDOWS, "iconic_windows": [65716]}],
        },
    )
    status = run.status()
    assert status.restore_capability == "iconic-unanswered"
    assert "IsIconic=unresolved" in (status.restore_error or "")
    assert "PostMessageW=unresolved" in (status.restore_error or "")
    assert not [message for message in _sent_messages(run) if message["message"] == 0x0112]


def test_bridge_falls_back_to_the_one_parameter_local_call(tmp_path: Path):
    """Cheat Engine's own scripts ask a user32 predicate the other way.

    `ceshare/ceshare_publish.lua` calls `executeCodeLocal('IsWindowVisible',
    winhandle)`, which is exactly this shape of call, while the many-parameter
    form is what the shipped scripts use for `DrawIconEx` and
    `ntdll.RtlGetVersion`. On the device the many-parameter form answered
    nothing at all for `IsIconic`, so both are tried.

    The question is then answered, and the request still cannot be sent: posting
    carries four parameters and only the many-parameter form takes them. That is
    reported as the half it actually is rather than as an unanswerable question.
    """
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "main_form_handle": 1024,
            "no_execute_code_local_ex": True,
            "steps": [{"ticks": 12, "game_windows": GAME_WINDOWS, "iconic_windows": [65716]}],
        },
    )
    assert run.status().restore_capability == "no-post"
    assert not [message for message in _sent_messages(run) if message["message"] == 0x0112]


def test_bridge_reports_what_the_refused_local_call_said(tmp_path: Path):
    # Without this the only thing a report could say is that the call did not
    # answer, which is what the first report of this on the device did say, and
    # it is not enough to tell a missing symbol from a call that failed.
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "main_form_handle": 1024,
            "execute_code_local_error": True,
            "steps": [{"ticks": 12, "game_windows": GAME_WINDOWS, "iconic_windows": [65716]}],
        },
    )
    status = run.status()
    assert status.restore_capability == "iconic-unanswered"
    assert "local call failed" in (status.restore_error or "")
    # The first refusal is kept, so the reason names a call and a symbol.
    assert "IsIconic" in (status.restore_error or "")


def test_bridge_sends_nothing_when_the_game_cannot_be_asked_back(tmp_path: Path):
    # Seeing that a game is minimized and having no way to ask it back is not a
    # capability, and it has to read the same as not being able to see it: the
    # panel would otherwise say the question could be answered while nothing
    # ever happened.
    # Losing the symbol table entirely is not the same as losing one symbol in
    # it: nothing can be resolved then, including the call that answers the
    # question, so that case stops earlier and says so.
    for scenario, capability in (
        ({"no_post_message": True, "no_self_module_list": True}, "no-post"),
        ({"no_get_address": True, "no_self_module_list": True}, "iconic-unanswered"),
    ):
        run = _run(
            tmp_path,
            descriptor=_descriptor(),
            scenario={
                "target_process": "game.exe",
                "target_pid": 4321,
                "main_form_handle": 1024,
                **scenario,
                "steps": [{"ticks": 96, "game_windows": GAME_WINDOWS, "iconic_windows": [65716]}],
            },
        )
        assert run.status().minimized_query == "unavailable", scenario
        # And it says which half that was. This Cheat Engine answers the
        # minimized question perfectly well; what it cannot do is post the
        # request, and a report that blamed the question would send whoever
        # read it to look at a call that works.
        assert run.status().restore_capability == capability, scenario
        assert not [
            message for message in _sent_messages(run)
            if message["message"] == 0x0112
        ], scenario


def test_bridge_does_not_count_a_restore_the_post_refused(tmp_path: Path):
    # A request that was never queued is not one the game has failed to answer,
    # so it is not recorded as asked and the window may be asked again.
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "main_form_handle": 1024,
            "post_message_refused": True,
            "steps": [{"ticks": 12, "game_windows": GAME_WINDOWS, "iconic_windows": [65716]}],
        },
    )
    assert run.status().game_restores is None
    assert not [
        message for message in _sent_messages(run)
        if message["message"] == 0x0112
    ]


def test_bridge_asks_the_game_nothing_when_it_is_not_attached(tmp_path: Path):
    # Nothing is minimized on behalf of a game this bridge never opened.
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        scenario={
            "target_process": "missing.exe",
            "steps": [{"ticks": 12, "game_windows": GAME_WINDOWS}],
        },
    )

    assert run.status().game_restores is None
    assert not [
        message for message in _sent_messages(run)
        if message["message"] == 0x0112
    ]


def test_bridge_gives_the_hide_a_sweep_before_closing_anything(tmp_path: Path):
    # Hiding is harmless and closing answers a question on the user's behalf, so
    # a window that goes down on its own is never closed. One sweep of patience
    # is what separates the two.
    descriptor = _descriptor()
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "steps": [
                {"ticks": 2},
                {"foreground_window": {"handle": 4096, "caption": "Confirmation"}, "ticks": 4},
                {"foreground_window": False, "ticks": 8},
            ],
        },
    )
    status = run.status()
    assert status.dialogs_dismissed is None
    sent = next(line for line in run.stdout.splitlines() if line.startswith("#SENTMESSAGES#"))
    assert sent.split("\t")[1] == "0"


def test_bridge_never_closes_a_window_that_belongs_to_the_game(tmp_path: Path):
    # The game runs in the same Wine prefix, so its own windows are in the same
    # foreground list. Only windows this process owns are Cheat Engine's to
    # close, and the game's are never touched.
    descriptor = _descriptor()
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "ce_process_id": 4242,
            "steps": [
                {"ticks": 2},
                {"foreground_window": {"handle": 8192, "caption": "Game", "pid": 777}, "ticks": 12},
            ],
        },
    )
    status = run.status()
    assert status.dialogs_dismissed is None
    sent = next(line for line in run.stdout.splitlines() if line.startswith("#SENTMESSAGES#"))
    assert sent.split("\t")[1] == "0"


def test_bridge_never_closes_the_main_form_even_while_it_is_hidden(tmp_path: Path):
    # Wine leaves the foreground on a window that was hidden rather than
    # destroyed, so the main form can be both down and in the foreground.
    # Closing it would end the session, and "the main form is hidden" is not
    # evidence that this handle is something else.
    descriptor = _descriptor()
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "main_form_handle": 1024,
            "steps": [
                {"ticks": 2},
                {"foreground_window": {"handle": 1024, "caption": "Cheat Engine 7.7"}, "ticks": 12},
            ],
        },
    )
    status = run.status()
    assert status.dialogs_dismissed is None
    sent = next(line for line in run.stdout.splitlines() if line.startswith("#SENTMESSAGES#"))
    assert sent.split("\t")[1] == "0"


def test_bridge_falls_back_to_the_caption_when_the_handle_cannot_be_read(tmp_path: Path):
    # `Handle` is documented, but a build that will not answer for it must not
    # cost the identity check: the caption still separates the main form from a
    # dialog, and a dialog is still dismissed.
    descriptor = _descriptor()
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "main_form_without_handle": True,
            "steps": [
                {"ticks": 2},
                {"foreground_window": {"handle": 4096, "caption": "Cheat Engine 7.7"}, "ticks": 8},
                {"foreground_window": {"handle": 8192, "caption": "Confirmation"}, "ticks": 8},
            ],
        },
    )
    status = run.status()
    # The window wearing the main form's caption was left alone; the dialog was
    # not.
    assert status.dialogs_dismissed == 1
    assert status.last_dialog == "Confirmation"


def test_bridge_closes_nothing_when_it_cannot_tell_what_the_window_is(tmp_path: Path):
    # Neither handle nor caption readable: the sweep cannot say whether this is
    # the main form, so it does not guess. Hiding keeps running.
    descriptor = _descriptor()
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "main_form_without_handle": True,
            "main_form_without_caption": True,
            "steps": [
                {"ticks": 2},
                {"foreground_window": {"handle": 4096, "caption": "Confirmation"}, "ticks": 12},
            ],
        },
    )
    status = run.status()
    assert status.dialogs_dismissed is None
    sent = next(line for line in run.stdout.splitlines() if line.startswith("#SENTMESSAGES#"))
    assert sent.split("\t")[1] == "0"


def test_bridge_publishes_a_long_non_ascii_caption_without_breaking_the_status(tmp_path: Path):
    # The status is decoded as UTF-8 and one invalid value fails the whole
    # parse, which reports a healthy attached session as unreadable protocol
    # state. A caption is arbitrary text, so the byte limit has to cut on a
    # character boundary.
    descriptor = _descriptor()
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "steps": [
                {"ticks": 2},
                {"foreground_window": {"handle": 4096, "caption": "Подтверждение " * 40}, "ticks": 12},
            ],
        },
    )
    status = run.status()
    assert status.dialogs_dismissed == 1
    caption = status.last_dialog or ""
    assert caption.startswith("Подтверждение")
    assert len(caption.encode("utf-8")) <= 256


def test_bridge_answers_a_stubborn_dialog_once_and_says_so_once(tmp_path: Path):
    # A window that ignores the close would otherwise be closed again every
    # second, and the count would describe a session that met a thousand
    # dialogs instead of the one it actually met.
    descriptor = _descriptor()
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "ignore_window_close": True,
            "steps": [
                {"ticks": 2},
                {"foreground_window": {"handle": 4096, "caption": "Confirmation"}, "ticks": 24},
            ],
        },
    )
    status = run.status()
    assert status.dialogs_dismissed == 1
    sent = next(line for line in run.stdout.splitlines() if line.startswith("#SENTMESSAGES#"))
    assert sent.split("\t")[1] == "1"


def test_bridge_will_not_close_anything_while_the_main_form_is_up(tmp_path: Path):
    # Closing the main form ends the session. A foreground window this process
    # owns either is the main form or is not, and while the main form cannot be
    # proven down that question has no answer, so nothing is closed - the hide
    # keeps running instead.
    descriptor = _descriptor()
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "unhidable_main_form": True,
            "steps": [
                {"ticks": 2},
                {"foreground_window": {"handle": 4096, "caption": "Confirmation"}, "ticks": 12},
            ],
        },
    )
    status = run.status()
    assert status.dialogs_dismissed is None
    sent = next(line for line in run.stdout.splitlines() if line.startswith("#SENTMESSAGES#"))
    assert sent.split("\t")[1] == "0"


def test_bridge_hides_when_one_form_cannot_be_read_rather_than_assuming_it_is_down(tmp_path: Path):
    # A getter that throws is not evidence the window is down. Collapsing it to
    # "hidden" let the sweep conclude everything was already down while one form
    # could not be looked at, and skip the hide on that basis.
    descriptor = _descriptor()
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "extra_forms": 2,
            "form_visibility_error": 2,
            "steps": [{"ticks": 12}],
        },
    )
    mainform = next(line for line in run.stdout.splitlines() if line.startswith("#MAINFORM#"))
    hides = int(mainform.split("\t")[2])
    # One for the first show, then one per sweep for as long as the form set
    # cannot be proven down.
    assert hides >= 3
    # An uncertain sweep is not a window anyone saw, so it must not be counted:
    # that number is the evidence for a game losing its audio.
    assert run.status().window_suppressions == 0


def test_bridge_does_not_fight_a_cheat_engine_that_keeps_its_windows_down(tmp_path: Path):
    # The sweep must cost nothing while CE behaves: a session that never maps a
    # window reports no suppressions at all, so any non-zero count is real
    # evidence about the thing that takes a game's audio.
    descriptor = _descriptor()
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={"target_process": "game.exe", "extra_forms": 2, "steps": [{"ticks": 12}]},
    )
    assert run.status().window_suppressions == 0


# -- descriptor integrity ---------------------------------------------------


def test_bridge_refuses_a_descriptor_whose_md5_does_not_match_before_reading(tmp_path: Path):
    descriptor = _descriptor()
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={"target_process": "game.exe", "md5": ["f" * 32], "steps": [{"ticks": 2}]},
    )
    assert run.status_bytes is None
    assert any("integrity check failed before read" in line for line in run.diagnostics)
    assert "#MAINFORM#\t1\t2\tfalse" in run.stdout
    assert "#APPLICATION#\tfalse" in run.stdout


def test_bridge_refuses_a_descriptor_that_changes_between_the_two_hash_checks(tmp_path: Path):
    descriptor = _descriptor()
    digest = md5(descriptor).hexdigest()
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={"target_process": "game.exe", "md5": [digest, "f" * 32], "steps": [{"ticks": 2}]},
    )
    assert run.status_bytes is None
    assert any("integrity check failed after read" in line for line in run.diagnostics)


def test_bridge_refuses_a_descriptor_with_an_unknown_field(tmp_path: Path):
    descriptor = _descriptor() + b"F\tunexpected\tvalue\n"
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={"target_process": "game.exe", "steps": [{"ticks": 2}]},
    )
    assert run.status_bytes is None
    assert any("unknown descriptor field" in line for line in run.diagnostics)


def test_bridge_refuses_more_startup_actions_than_the_protocol_allows(tmp_path: Path):
    descriptor = _descriptor() + b"".join(
        f"A\t{record_id}\tactive\t1\n".encode("ascii")
        for record_id in range(MAX_STARTUP_ACTIONS + 1)
    )
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={"target_process": "game.exe", "steps": [{"ticks": 2}]},
    )
    assert run.status_bytes is None
    assert any("too many startup actions" in line for line in run.diagnostics)


def test_bridge_refuses_a_descriptor_with_a_non_wine_path(tmp_path: Path):
    # Descriptor fields are percent-encoded, so replace the encoded form.
    descriptor = _descriptor().replace(
        percent_encode("Z:" + chr(92) + "table.ct").encode("ascii"),
        percent_encode("/tmp/table.ct").encode("ascii"),
    )
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={"target_process": "game.exe", "steps": [{"ticks": 2}]},
    )
    assert run.status_bytes is None
    assert any("bounded Wine Z: paths" in line for line in run.diagnostics)


def test_bridge_refuses_an_invalid_descriptor_environment(tmp_path: Path):
    descriptor = _descriptor()
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={"target_process": "game.exe", "steps": [{"ticks": 1}]},
        descriptor_sha256="not-a-digest",
    )
    assert run.status_bytes is None
    assert any("descriptor environment is missing or invalid" in line for line in run.diagnostics)


# -- attach, startup and heartbeat -----------------------------------------


def test_bridge_attaches_applies_startup_and_writes_a_parsable_status(tmp_path: Path):
    descriptor = _descriptor(
        startup=(
            StartupAction(record_id=1, kind="active", value="1", path=()),
            StartupAction(record_id=2, kind="value", value="1234", path=()),
        )
    )
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "records": {1: {"active": False}, 2: {"value": "0"}},
            "steps": [{"ticks": 3}],
        },
    )
    status = run.status()
    assert status.session_id == SESSION_ID
    assert status.app_id == 220
    assert status.ce_sha256 == CE_SHA
    assert status.table_sha256 == TABLE_SHA
    assert status.descriptor_sha256 == sha256(descriptor).hexdigest()
    assert status.attached is True
    assert status.opened_process_id == 4321
    assert status.target_process == "game.exe"
    startup_results = {result.record_id: result for result in status.results if result.generation == 0}
    assert startup_results[1].ok is True and startup_results[1].active is True
    assert startup_results[2].ok is True and startup_results[2].value == "1234"


def test_bridge_keeps_the_last_good_status_when_the_next_write_fails(tmp_path: Path):
    """A full disk must cost one heartbeat, not the whole protocol file.

    The writer used to unlink the current status and rename a staged temp file
    into place without checking a single result, so an ENOSPC/I/O failure
    published a truncated status - or no status at all - and the host could only
    report that as parse failure on an otherwise healthy session.
    """
    descriptor = _descriptor()
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "steps": [
                {"ticks": 2},
                {"write_failures": {"Z:\\status.txt.tmp": True}, "ticks": 3},
            ],
        },
    )
    status = run.status()
    assert status.session_id == SESSION_ID
    assert status.attached is True
    assert not (run.root / "status.txt.tmp").exists(), "a rejected staging file must not be left behind"


def test_bridge_keeps_the_last_good_status_when_the_final_rename_fails(tmp_path: Path):
    """A replacement that never lands must not cost the previous heartbeat.

    Removing the current status before the rename turned a failed promotion into
    a session with no heartbeat at all, which the host can only report as
    protocol corruption on an otherwise healthy bridge.
    """
    descriptor = _descriptor()
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "steps": [
                {"ticks": 2},
                {"rename_failures": {"Z:\\status.txt.tmp": True}, "ticks": 3},
            ],
        },
    )
    status = run.status()
    assert status.session_id == SESSION_ID
    assert status.attached is True
    assert not (run.root / "status.txt.tmp").exists(), "a rejected staging file must not be left behind"
    assert not (run.root / "status.txt.old").exists(), "the displaced heartbeat must be restored, not kept aside"


def test_bridge_reports_a_startup_activation_that_never_settles_as_failed(tmp_path: Path):
    descriptor = _descriptor(startup=(StartupAction(record_id=1, kind="active", value="1", path=()),))
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "records": {1: {"active": False, "activation_never_settles": True}},
            "steps": [{"ticks": 3}],
        },
    )
    result = next(item for item in run.status().results if item.generation == 0 and item.record_id == 1)
    assert result.ok is False
    assert result.active is False
    assert result.error == "activation did not settle"
    # Nobody reported an error and the record is still off, so this is Cheat
    # Engine declining to run it rather than an undifferentiated failure.
    assert result.error_code == "activation_rejected"


def test_bridge_waits_for_async_startup_activation_before_continuing(tmp_path: Path):
    descriptor = _descriptor(startup=(
        StartupAction(record_id=1, kind="active", value="1", path=()),
        StartupAction(record_id=9, kind="active", value="1", path=()),
    ))
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "records": {
                1: {"active": False, "activation_async_ticks": 2},
                9: {"missing": True, "created_by": 1, "active": False},
            },
            "steps": [{"ticks": 5}],
        },
    )
    results = {(item.generation, item.record_id): item for item in run.status().results}
    assert results[(0, 1)].ok is True and results[(0, 1)].active is True
    assert results[(0, 9)].ok is True and results[(0, 9)].active is True
    assert run.status().startup_state == "applied"


def test_bridge_bounds_an_async_startup_activation_that_never_finishes(tmp_path: Path):
    descriptor = _descriptor(startup=(StartupAction(record_id=1, kind="active", value="1", path=()),))
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "records": {1: {"active": False, "activation_async_never_settles": True}},
            "steps": [{"ticks": 95}],
        },
    )
    result = next(item for item in run.status().results if item.generation == 0 and item.record_id == 1)
    assert result.ok is False
    assert result.error == "activation timed out"
    # The restore of that same record goes asynchronous too and never settles,
    # so the bridge cannot prove the record is back and says so rather than
    # claiming a clean abort.
    assert run.status().startup_state == "failed_partial"


def test_bridge_rolls_back_earlier_startup_actions_after_a_later_failure(tmp_path: Path):
    descriptor = _descriptor(startup=(
        StartupAction(record_id=1, kind="active", value="1", path=()),
        StartupAction(record_id=2, kind="active", value="1", path=()),
    ))
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "records": {1: {"active": False}, 2: {"active": False, "activation_never_settles": True}},
            "steps": [{"ticks": 3}],
        },
        controls={"control.txt": _control(RuntimeCommand(generation=1, kind="query", record_id=1, value=None))},
    )
    results = {(item.generation, item.record_id): item for item in run.status().results}
    assert results[(0, 2)].ok is False
    # The control runs after startup and proves the successful first action was
    # restored rather than silently leaving a partial startup state behind.
    assert results[(1, 1)].ok is True and results[(1, 1)].active is False


def test_bridge_waits_for_an_async_rollback_before_calling_the_startup_rolled_back(tmp_path: Path):
    """Rollback is held to the forward path's confirmation standard.

    Restoring an Auto Assembler script is asynchronous exactly as switching it
    on is, and the rollback used to be a bare unchecked assignment: it never
    waited, never re-read, and the failure it reported said nothing about
    whether the game had actually been put back.
    """
    descriptor = _descriptor(startup=(
        StartupAction(record_id=1, kind="active", value="1", path=()),
        StartupAction(record_id=2, kind="active", value="1", path=()),
    ))
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "records": {
                1: {"active": False, "activation_async_ticks": 1},
                2: {"active": False, "activation_never_settles": True},
            },
            # The probe runs only after the rollback has had time to settle, so
            # it reports the state the rollback actually reached.
            "steps": [{"ticks": 8}, {"control": "probe.txt", "ticks": 3}],
        },
        controls={"probe.txt": _control(RuntimeCommand(generation=1, kind="query", record_id=1, value=None))},
    )
    status = run.status()
    assert status.startup_state == "failed_rolled_back"
    results = {(item.generation, item.record_id): item for item in status.results}
    assert results[(0, 2)].ok is False
    assert results[(1, 1)].ok is True and results[(1, 1)].active is False


def test_bridge_reports_a_rollback_it_could_not_prove_as_partial(tmp_path: Path):
    """An unproven rollback must not look like a clean abort.

    An enclosing script left patched into the game after a "failed" startup is
    exactly the residue a user cannot see: CE Decky hides plugin-managed scripts
    from the remembered selection on purpose.
    """
    descriptor = _descriptor(startup=(
        StartupAction(record_id=1, kind="active", value="1", path=()),
        StartupAction(record_id=2, kind="active", value="1", path=()),
    ))
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "records": {
                1: {"active": False, "restore_never_settles": True},
                2: {"active": False, "activation_never_settles": True},
            },
            "steps": [{"ticks": 8}, {"control": "probe.txt", "ticks": 3}],
        },
        controls={"probe.txt": _control(RuntimeCommand(generation=1, kind="query", record_id=1, value=None))},
    )
    status = run.status()
    assert status.startup_state == "failed_partial"
    results = {(item.generation, item.record_id): item for item in status.results}
    # The script really is still applied, which is what the state now admits.
    assert results[(1, 1)].ok is True and results[(1, 1)].active is True


def test_bridge_rolls_back_the_failing_action_itself(tmp_path: Path):
    """The action that failed has usually already moved the record.

    A value the table clamps is written, read back as something else and
    reported failed - but it was written. Starting the rollback one action
    earlier skipped exactly that record, so `failed_rolled_back` could be
    claimed over a game the startup had changed.
    """
    descriptor = _descriptor(startup=(
        StartupAction(record_id=1, kind="active", value="1", path=()),
        StartupAction(record_id=2, kind="value", value="1234", path=()),
    ))
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            # Record 2 accepts the write and then reports a clamped value, which
            # is a real Cheat Engine outcome and a genuine failure.
            "records": {1: {"active": False}, 2: {"value": "0", "clamp_value": "99"}},
            "steps": [{"ticks": 6}, {"control": "probe.txt", "ticks": 3}],
        },
        controls={"probe.txt": _control(
            RuntimeCommand(generation=1, kind="query", record_id=1, value=None),
            RuntimeCommand(generation=2, kind="query", record_id=2, value=None),
        )},
    )
    status = run.status()
    results = {(item.generation, item.record_id): item for item in status.results}
    assert results[(0, 2)].ok is False
    # The earlier action was restored, and the clamping record cannot be, so the
    # bridge reports partial rather than a clean abort.
    assert results[(1, 1)].ok is True and results[(1, 1)].active is False
    assert results[(2, 2)].value == "99"
    assert status.startup_state == "failed_partial"


def test_bridge_publishes_startup_progress_beyond_its_result_window(tmp_path: Path):
    """Progress has to stay visible after the bounded result list fills.

    Every startup result carries generation 0 and the bridge keeps only the most
    recent 128, so a host counting results saw a large plan stop advancing while
    the bridge was still working - and reported a successful startup as pending.
    """
    count = 130
    descriptor = _descriptor(startup=tuple(
        StartupAction(record_id=index, kind="active", value="1", path=())
        for index in range(1, count + 1)
    ))
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "records": {index: {"active": False} for index in range(1, count + 1)},
            "steps": [{"ticks": count + 5}],
        },
    )
    status = run.status()
    assert status.startup_state == "applied"
    startup_results = [item for item in status.results if item.generation == 0]
    # The window is still bounded, which is exactly why the counter is separate.
    assert len(startup_results) < count
    assert status.startup_completed == count
    assert status.startup_total == count


def test_bridge_activates_the_enclosing_script_that_creates_the_next_record(tmp_path: Path):
    """A nested record does not exist until its enclosing script has run.

    The backend orders enclosing scripts before the records inside them, but the
    bridge used to resolve every startup record before executing any action. For
    exactly the tables that ordering exists for, the child lookup failed, the
    parent was never switched on, and each timer tick repeated the same
    impossible preflight forever.
    """
    descriptor = _descriptor(
        startup=(
            StartupAction(record_id=1, kind="active", value="1", path=()),
            StartupAction(record_id=9, kind="active", value="1", path=()),
        )
    )
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "records": {1: {"active": False}, 9: {"missing": True, "created_by": 1, "active": False}},
            "steps": [{"ticks": 4}],
        },
    )
    status = run.status()
    assert status.attached is True
    startup_results = {result.record_id: result for result in status.results if result.generation == 0}
    assert startup_results[1].ok is True and startup_results[1].active is True
    assert startup_results[9].ok is True and startup_results[9].active is True


def test_bridge_publishes_startup_state_so_autoload_can_tell_success_from_waiting(tmp_path: Path):
    """A fresh heartbeat proves the bridge is alive, not that cheats came back."""
    descriptor = _descriptor(
        startup=(
            StartupAction(record_id=1, kind="active", value="1", path=()),
            StartupAction(record_id=9, kind="active", value="1", path=()),
        )
    )
    waiting = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "records": {1: {"active": False}, 9: {"missing": True, "created_by": 1, "active": False}},
            "steps": [{"ticks": 1}],
        },
    )
    assert waiting.status().startup_state in {"pending", "applied"}

    settled = _run(
        tmp_path / "settled",
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "records": {1: {"active": False}, 9: {"missing": True, "created_by": 1, "active": False}},
            "steps": [{"ticks": 5}],
        },
    )
    assert settled.status().startup_state == "applied"

    broken = _run(
        tmp_path / "broken",
        descriptor=_descriptor(startup=(StartupAction(record_id=2, kind="value", value="1234", path=()),)),
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "records": {2: {"value": "0", "clamp_value": "99"}},
            "steps": [{"ticks": 3}],
        },
    )
    # The failing action is itself rolled back now, and this table clamps every
    # write, so restoring the recorded value produces the clamp again: the
    # record really is still changed and the state admits it.
    assert broken.status().startup_state == "failed_partial"


def test_bridge_reports_a_startup_record_that_never_appears_with_its_exact_id(tmp_path: Path):
    """Waiting is bounded: an absent record must not retry for the whole session."""
    descriptor = _descriptor(
        startup=(
            StartupAction(record_id=1, kind="active", value="1", path=()),
            StartupAction(record_id=9, kind="active", value="1", path=()),
        )
    )
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "records": {1: {"active": False}, 9: {"missing": True}},
            "steps": [{"ticks": 45}],
        },
    )
    status = run.status()
    failures = [result for result in status.results if result.generation == 0 and not result.ok]
    assert [failure.record_id for failure in failures] == [9]
    assert "did not appear" in (failures[0].error or "")


def test_bridge_startup_value_must_read_back_as_written(tmp_path: Path):
    """A table that clamps or coerces a saved value is not a restored cheat.

    Live Apply already requires read-back equality. Startup only required that
    the read itself succeed, so auto-load could report the previous session's
    cheats restored while the value silently became something else.
    """
    descriptor = _descriptor(startup=(StartupAction(record_id=2, kind="value", value="1234", path=()),))
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "records": {2: {"value": "0", "clamp_value": "99"}},
            "steps": [{"ticks": 3}],
        },
    )
    status = run.status()
    failures = [result for result in status.results if result.generation == 0 and not result.ok]
    assert [failure.record_id for failure in failures] == [2]
    assert "read back" in (failures[0].error or "")


def test_bridge_reports_an_unattached_target_without_failing(tmp_path: Path):
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        scenario={"target_process": "game.exe", "target_pid": 0, "steps": [{"ticks": 2}]},
    )
    status = run.status()
    assert status.attached is False
    assert status.opened_process_id == 0


def test_bridge_treats_a_mismatched_opened_process_as_unattached(tmp_path: Path):
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "open_process_mismatch": True,
            "steps": [{"ticks": 2}],
        },
    )
    assert run.status().attached is False


# -- control protocol -------------------------------------------------------


def test_bridge_executes_query_value_and_activation_commands_with_exact_acks(tmp_path: Path):
    controls = {
        "control-2.txt": _control(
            RuntimeCommand(generation=1, kind="query", record_id=1),
            RuntimeCommand(generation=2, kind="set_active", record_id=1, value="1"),
            RuntimeCommand(generation=3, kind="set_value", record_id=2, value="4242"),
        )
    }
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        controls=controls,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "records": {1: {"active": False, "value": "7"}, 2: {"value": "0"}},
            "steps": [{"ticks": 2}, {"control": "control-2.txt", "ticks": 2}],
        },
    )
    results = {result.generation: result for result in run.status().results}
    assert results[1].record_id == 1 and results[1].ok is True and results[1].active is False
    assert results[2].record_id == 1 and results[2].ok is True and results[2].active is True
    assert results[3].record_id == 2 and results[3].ok is True and results[3].value == "4242"


def test_bridge_serializes_live_commands_behind_async_activation(tmp_path: Path):
    controls = {
        "control-2.txt": _control(
            RuntimeCommand(generation=1, kind="set_active", record_id=1, value="1"),
            RuntimeCommand(generation=2, kind="set_active", record_id=9, value="1"),
        )
    }
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        controls=controls,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "records": {
                1: {"active": False, "activation_async_ticks": 2},
                9: {"missing": True, "created_by": 1, "active": False},
            },
            "steps": [{"ticks": 1}, {"control": "control-2.txt", "ticks": 5}],
        },
    )
    results = {result.generation: result for result in run.status().results}
    assert results[1].ok is True and results[1].active is True
    assert results[2].ok is True and results[2].active is True


def test_bridge_never_re_executes_an_already_acknowledged_generation(tmp_path: Path):
    controls = {
        "control-2.txt": _control(RuntimeCommand(generation=1, kind="set_value", record_id=2, value="11")),
        "control-3.txt": _control(RuntimeCommand(generation=1, kind="set_value", record_id=2, value="99")),
    }
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        controls=controls,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "records": {2: {"value": "0"}},
            "steps": [
                {"ticks": 1},
                {"control": "control-2.txt", "ticks": 2},
                {"control": "control-3.txt", "ticks": 2},
            ],
        },
    )
    results = [result for result in run.status().results if result.generation == 1]
    assert len(results) == 1
    assert results[0].value == "11"


def test_bridge_refuses_record_commands_while_unattached(tmp_path: Path):
    controls = {"control-2.txt": _control(RuntimeCommand(generation=1, kind="query", record_id=1))}
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        controls=controls,
        scenario={
            "target_process": "game.exe",
            "target_pid": 0,
            "records": {1: {"active": True}},
            "steps": [{"ticks": 1}, {"control": "control-2.txt", "ticks": 2}],
        },
    )
    result = next(item for item in run.status().results if item.generation == 1)
    assert result.ok is False
    assert result.error == "target process is not attached"
    assert result.error_code == "target_detached"


def test_bridge_reports_a_missing_memory_record_instead_of_inventing_one(tmp_path: Path):
    controls = {"control-2.txt": _control(RuntimeCommand(generation=1, kind="query", record_id=77))}
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        controls=controls,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "records": {77: {"missing": True}},
            "steps": [{"ticks": 1}, {"control": "control-2.txt", "ticks": 2}],
        },
    )
    result = next(item for item in run.status().results if item.generation == 1)
    assert result.ok is False
    assert result.error == "MemoryRecord missing"
    assert result.error_code == "record_missing"


def test_bridge_lists_processes_and_percent_encodes_unicode_names(tmp_path: Path):
    controls = {"control-2.txt": _control(RuntimeCommand(generation=1, kind="list_processes"))}
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        controls=controls,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "processes": {7: "game.exe", 9: "Zażółć 日本.exe"},
            "steps": [{"ticks": 1}, {"control": "control-2.txt", "ticks": 2}],
        },
    )
    status = run.status()
    assert {7: "game.exe", 9: "Zażółć 日本.exe"}.items() <= dict(status.processes).items()
    assert next(item for item in status.results if item.generation == 1).ok is True


def test_bridge_changes_target_only_through_an_acknowledged_retry_attach(tmp_path: Path):
    controls = {
        "control-2.txt": _control(
            RuntimeCommand(generation=1, kind="retry_attach", value="launcher.exe", target_pid=555),
        )
    }
    run = _run(
        tmp_path,
        descriptor=_descriptor(target_process="game.exe"),
        controls=controls,
        scenario={
            "target_process": "launcher.exe",
            "target_pid": 555,
            "steps": [{"ticks": 2}, {"control": "control-2.txt", "ticks": 2}],
        },
    )
    status = run.status()
    assert status.target_process == "launcher.exe"
    assert status.attached is True
    assert status.opened_process_id == 555
    assert next(item for item in status.results if item.generation == 1).ok is True


def test_bridge_refuses_ambiguous_auto_attach_but_accepts_an_exact_observed_pid(tmp_path: Path):
    controls = {
        "control-2.txt": _control(RuntimeCommand(generation=1, kind="retry_attach", value="game.exe", target_pid=22)),
    }
    run = _run(
        tmp_path,
        descriptor=_descriptor(target_process="game.exe"),
        controls=controls,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "processes": {11: "game.exe", 22: "GAME.EXE"},
            "steps": [{"ticks": 2}, {"control": "control-2.txt", "ticks": 2}],
        },
    )
    status = run.status()
    assert status.attached is True
    assert status.opened_process_id == 22
    assert next(item for item in status.results if item.generation == 1).ok is True


def test_bridge_ignores_a_control_file_with_a_wrong_header(tmp_path: Path):
    controls = {"control-2.txt": b"CEDECKY-CONTROL-9\nC\t1\tquery\t1\t-\n"}
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        controls=controls,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "records": {1: {"active": True}},
            "steps": [{"ticks": 1}, {"control": "control-2.txt", "ticks": 2}],
        },
    )
    assert [item for item in run.status().results if item.generation == 1] == []


def test_bridge_ignores_a_control_file_with_non_increasing_generations(tmp_path: Path):
    controls = {"control-2.txt": b"CEDECKY-CONTROL-1\nC\t2\tquery\t1\t-\nC\t2\tquery\t1\t-\n"}
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        controls=controls,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "records": {1: {"active": True}},
            "steps": [{"ticks": 1}, {"control": "control-2.txt", "ticks": 2}],
        },
    )
    assert [item for item in run.status().results if item.generation == 2] == []


def test_bridge_ignores_more_commands_than_the_protocol_allows(tmp_path: Path):
    oversized = b"CEDECKY-CONTROL-1\n" + b"".join(
        f"C\t{generation}\tquery\t1\t-\t-\n".encode("ascii")
        for generation in range(1, MAX_COMMANDS + 2)
    )
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        controls={"control-2.txt": oversized},
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "records": {1: {"active": True}},
            "steps": [{"ticks": 1}, {"control": "control-2.txt", "ticks": 2}],
        },
    )
    assert [item for item in run.status().results if item.generation > 0] == []


def test_bridge_ignores_an_unsupported_command_kind(tmp_path: Path):
    controls = {"control-2.txt": b"CEDECKY-CONTROL-1\nC\t1\tload_table\t-\tZ%3A%5Cother.ct\n"}
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        controls=controls,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "steps": [{"ticks": 1}, {"control": "control-2.txt", "ticks": 2}],
        },
    )
    assert [item for item in run.status().results if item.generation == 1] == []


def test_bridge_rejects_a_control_file_carrying_a_forbidden_control_character(tmp_path: Path):
    controls = {"control-2.txt": b"CEDECKY-CONTROL-1\r\nC\t1\tquery\t1\t-\n"}
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        controls=controls,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "records": {1: {"active": True}},
            "steps": [{"ticks": 1}, {"control": "control-2.txt", "ticks": 2}],
        },
    )
    assert [item for item in run.status().results if item.generation == 1] == []


def test_bridge_status_survives_a_record_read_failure(tmp_path: Path):
    controls = {"control-2.txt": _control(RuntimeCommand(generation=1, kind="set_value", record_id=3, value="5"))}
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        controls=controls,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "records": {3: {"value": "0", "value_error": True}},
            "steps": [{"ticks": 1}, {"control": "control-2.txt", "ticks": 2}],
        },
    )
    result = next(item for item in run.status().results if item.generation == 1)
    assert result.ok is False
    assert result.error is not None


def test_bridge_round_trips_protocol_characters_through_the_status_encoding(tmp_path: Path):
    payload = "a\tb\nc%d Zażółć"
    controls = {"control-2.txt": _control(RuntimeCommand(generation=1, kind="query", record_id=5))}
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        controls=controls,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "records": {5: {"value": payload}},
            "steps": [{"ticks": 1}, {"control": "control-2.txt", "ticks": 2}],
        },
    )
    result = next(item for item in run.status().results if item.generation == 1)
    assert result.ok is True
    assert result.value == payload


def test_bridge_replaces_an_oversized_value_with_a_bounded_marker(tmp_path: Path):
    controls = {"control-2.txt": _control(RuntimeCommand(generation=1, kind="query", record_id=6))}
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        controls=controls,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "records": {6: {"value": "x" * 4096}},
            "steps": [{"ticks": 1}, {"control": "control-2.txt", "ticks": 2}],
        },
    )
    result = next(item for item in run.status().results if item.generation == 1)
    assert result.value == "<value too large>"


def test_bridge_keeps_the_result_history_bounded(tmp_path: Path):
    commands = [RuntimeCommand(generation=index, kind="query", record_id=1) for index in range(1, 141)]
    controls = {"control-2.txt": _control(*commands)}
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        controls=controls,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "records": {1: {"active": True, "value": "1"}},
            "steps": [{"ticks": 1}, {"control": "control-2.txt", "ticks": 2}],
        },
    )
    results = run.status().results
    assert len(results) == 128
    # The newest acknowledgements are the ones a caller can still be waiting for.
    assert results[-1].generation == 140
    assert results[0].generation == 13


def test_bridge_writes_status_atomically_without_leaving_a_temporary_file(tmp_path: Path):
    run = _run(
        tmp_path,
        descriptor=_descriptor(),
        scenario={"target_process": "game.exe", "target_pid": 4321, "steps": [{"ticks": 3}]},
    )
    assert run.status().attached is True
    assert not (run.root / "status.txt.tmp").exists()


# -- complete production chain ---------------------------------------------
#
# The tests above validate the bridge against hand-built protocol files. This
# one runs the whole shipped chain instead: the production service prepares a
# real session, the real bridge consumes it, and the production service accepts
# the status the bridge wrote. It needs POSIX Wine `Z:` mapping, so it is the
# Linux gate's evidence.


def _service(tmp_path: Path, app_id: int):
    import logging
    import struct

    from ce_decky.paths import PluginPaths
    from ce_decky.service import PluginService

    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("bridge-e2e"))
    service.initialize()

    executable = paths.user_home / "CE" / "Cheat Engine.exe"
    executable.parent.mkdir(parents=True, exist_ok=True)
    data = bytearray(300_000)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE" + bytes(2)
    executable.write_bytes(bytes(data))
    service.import_ce(str(executable))

    source = tmp_path / "bridge-e2e.CT"
    source.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<CheatTable CheatEngineTableVersion="45">
  <CheatEntries>
    <CheatEntry><ID>1</ID><Description>Health</Description>
      <VariableType>4 Bytes</VariableType><Address>game.exe+1234</Address></CheatEntry>
    <CheatEntry><ID>2</ID><Description>Ammo</Description>
      <VariableType>4 Bytes</VariableType><Address>game.exe+2222</Address></CheatEntry>
  </CheatEntries>
</CheatTable>
""",
        encoding="utf-8",
    )
    table = service.import_table(str(source))
    service.save_profile(app_id, "Game", False, table["sha256"], "game.exe")
    service.set_execution_consent(app_id, table["sha256"], True)
    return service, table


def _run_prepared(prepared: dict, scenario: dict, tmp_path: Path) -> BridgeRun:
    """Drive the bridge against session files the production service wrote."""
    interpreter = _interpreter()
    # Wine `Z:` maps to the POSIX root, so the stub needs no relocation root.
    scenario = {"root": "", **scenario}
    scenario_path = tmp_path / f"scenario-{len(list(tmp_path.glob('scenario-*.lua')))}.lua"
    scenario_path.write_text("return " + _lua_value(scenario) + "\n", encoding="utf-8")
    environment = dict(os.environ)
    environment.update({
        "CE_DECKY_DESCRIPTOR": prepared["descriptor_windows_path"],
        "CE_DECKY_DESCRIPTOR_SHA256": prepared["descriptor_sha256"],
        "CE_DECKY_DESCRIPTOR_MD5": prepared["descriptor_md5"],
    })
    completed = subprocess.run(
        [interpreter, str(RUNNER), str(STUB), str(scenario_path), str(BRIDGE)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(ROOT),
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return BridgeRun(Path(prepared["status_path"]).parent, completed.stdout, completed.stderr)


@pytest.mark.skipif(os.name != "posix", reason="Wine Z: session paths require POSIX")
def test_production_service_accepts_the_status_the_real_bridge_writes(tmp_path: Path):
    service, _ = _service(tmp_path, 940)
    prepared = service.prepare_session(940)

    run = _run_prepared(
        prepared,
        {
            "target_process": "game.exe",
            "target_pid": 3131,
            "md5_default": prepared["descriptor_md5"],
            "records": {1: {"active": False}, 2: {"value": "5"}},
            "processes": {3131: "game.exe"},
            "loaded_table_path": parse_descriptor(Path(prepared["descriptor_path"]).read_bytes()).table_path,
            "steps": [{"ticks": 3}],
        },
        tmp_path,
    )
    assert run.loaded is True, run.diagnostics

    runtime = service.get_runtime_status(940)
    assert runtime["connected"] is True, runtime
    assert runtime["session_current"] is True
    status = runtime["status"]
    assert status["attached"] is True
    assert status["opened_process_id"] == 3131
    assert status["session_id"] == prepared["session_id"]
    assert status["descriptor_sha256"] == prepared["descriptor_sha256"]
    assert status["table_sha256"] == prepared["table_sha256"]
    assert status["target_process"] == "game.exe"
    assert status["address_list_count"] == 2
    assert status["table_load_state"] == "loaded"
    # The bridge only snapshots the process list when it is commanded to.
    assert not status["processes"]


@pytest.mark.skipif(os.name != "posix", reason="Wine Z: session paths require POSIX")
def test_production_runtime_command_is_written_executed_and_acknowledged(tmp_path: Path):
    service, _ = _service(tmp_path, 941)
    prepared = service.prepare_session(941)
    scenario = {
        "target_process": "game.exe",
        "target_pid": 3232,
        "md5_default": prepared["descriptor_md5"],
        "records": {1: {"active": False}, 2: {"value": "5"}},
        "processes": {3232: "game.exe"},
        "steps": [{"ticks": 3}],
    }
    _run_prepared(prepared, scenario, tmp_path)
    assert service.get_runtime_status(941)["connected"] is True

    # The production writer decides the generation and the control encoding.
    written = service.write_runtime_commands(941, [
        {"generation": 1, "kind": "set_active", "record_id": 1, "value": "1"},
    ])
    assert written["ok"] is True

    # A second bridge start consumes the control file the service just wrote.
    _run_prepared(prepared, scenario, tmp_path)
    runtime = service.get_runtime_status(941)
    acknowledged = [result for result in runtime["status"]["results"] if result["generation"] == 1]
    assert acknowledged, runtime["status"]["results"]
    assert acknowledged[0]["record_id"] == 1
    assert acknowledged[0]["ok"] is True
    assert acknowledged[0]["active"] is True


@pytest.mark.parametrize("extra,reason,attempts,successes", [
    ({}, "confirmed", 1, 1),
    ({"symbol_table_blind": True}, "confirmed", 1, 1),
    ({"focus_refused": True}, "refused", 6, 0),
    ({"focus_ex_refused": True}, "refused", 6, 0),
    ({"focus_error": True}, "call-error", 6, 0),
    ({"focus_hidden": True}, "no-window", 0, 0),
    ({"focus_available": False}, "unavailable", 0, 0),
])
def test_visible_game_focus_recovery_is_bounded_and_observed(tmp_path, extra, reason, attempts, successes):
    run = _run(tmp_path, descriptor=_descriptor(), scenario={
        "target_process": "game.exe", "target_pid": 4321, "main_form_handle": 1024,
        "focus_available": True, **extra,
        "steps": [{"ticks": 120, "game_windows": GAME_WINDOWS, "iconic_windows": [],
                   "foreground_window": {"handle": 1024, "caption": "Cheat Engine 7.7"}}],
    })
    status = run.status()
    assert status.focus_reason == reason
    assert status.focus_attempts == attempts
    assert status.focus_successes == successes
    assert status.game_restores is None


def test_focus_recovery_never_uses_foreign_or_ambiguous_windows(tmp_path):
    for name, windows, reason in [("foreign", [["GAME", 100, 99]], "no-window"),
                                  ("ambiguous", [["A", 100, 4321], ["B", 101, 4321]], "ambiguous")]:
        run = _run(tmp_path / name, descriptor=_descriptor(), scenario={
            "target_process": "game.exe", "target_pid": 4321, "main_form_handle": 1024,
            "focus_available": True,
            "steps": [{"ticks": 120, "game_windows": windows, "iconic_windows": []}],
        })
        assert run.status().focus_reason == reason
        assert run.status().focus_attempts == 0


@pytest.mark.parametrize("trigger", ["mutation", "dialog"])
def test_focus_recovery_rearms_only_for_new_ce_interaction(tmp_path, trigger):
    step = {"ticks": 120, "foreground_window": {"handle": 1024, "caption": "Cheat Engine 7.7"}}
    if trigger == "mutation":
        step["control"] = "activate.txt"
    else:
        step["foreground_window"] = {"handle": 4096, "caption": "Confirmation"}
    run = _run(tmp_path, descriptor=_descriptor(), scenario={
        "target_process": "game.exe", "target_pid": 4321, "main_form_handle": 1024,
        "focus_available": True, "records": {1: {"active": False}},
        "steps": [{"ticks": 120, "game_windows": GAME_WINDOWS, "iconic_windows": [],
                   "foreground_window": {"handle": 65716, "caption": "GAME", "pid": 4321}}, step],
    }, controls={"activate.txt": _control(RuntimeCommand(1, "set_active", 1, "1"))})
    assert run.status().focus_attempts == 1
    assert run.status().focus_successes == 1


def test_focus_counts_delayed_foreground_confirmation_once(tmp_path):
    run = _run(tmp_path, descriptor=_descriptor(), scenario={
        "target_process": "game.exe", "target_pid": 4321, "main_form_handle": 1024,
        "focus_available": True, "focus_delay_ticks": 1,
        "steps": [{"ticks": 120, "game_windows": GAME_WINDOWS, "iconic_windows": []}],
    })
    assert run.status().focus_attempts == 1
    assert run.status().focus_successes == 1
    assert run.status().focus_reason == "confirmed"


@pytest.mark.parametrize("rollback", [False, True])
def test_focus_rearms_after_long_async_startup_or_rollback(tmp_path, rollback):
    actions = [StartupAction(1, "active", "1", ())]
    if rollback:
        actions.append(StartupAction(2, "active", "1", ()))
    run = _run(tmp_path, descriptor=_descriptor(startup=tuple(actions)), scenario={
        "target_process": "game.exe", "target_pid": 4321, "main_form_handle": 1024,
        "focus_available": True,
        "records": {1: {"active": False, "activation_async_ticks": 30, "focus_on_settle": True},
                    2: {"active": False, "activation_never_settles": True}},
        "steps": [{"ticks": 140, "game_windows": GAME_WINDOWS, "iconic_windows": [],
                   "foreground_window": {"handle": 65716, "caption": "GAME", "pid": 4321}}],
    })
    assert run.status().startup_state == ("failed_rolled_back" if rollback else "applied")
    assert run.status().focus_attempts >= 1
    assert run.status().focus_successes >= 1


def test_focus_discovers_late_exports_after_restore_is_already_ready(tmp_path):
    run = _run(tmp_path, descriptor=_descriptor(), scenario={
        "target_process": "game.exe", "target_pid": 4321, "main_form_handle": 1024,
        "focus_available": True, "focus_exports_after_tick": 40,
        "steps": [{"ticks": 120, "game_windows": GAME_WINDOWS, "iconic_windows": []}],
    })
    status = run.status()
    assert status.restore_capability == "ready"
    assert status.focus_discovery_attempts > 6
    assert status.focus_attempts == status.focus_successes == 1


def test_startup_active_summary_outlives_diagnostic_result_ring(tmp_path):
    actions = (StartupAction(1, "active", "1", (), True),) + tuple(StartupAction(i, "value", "2", ()) for i in range(2, 152))
    run = _run(tmp_path, descriptor=_descriptor(startup=actions), scenario={
        "target_process": "game.exe", "target_pid": 4321,
        "records": {i: {"active": False, "value": "1"} for i in range(1, 152)},
        "steps": [{"ticks": 8}],
    })
    status = run.status()
    assert status.startup_state == "applied"
    assert len(status.results) == 128 and all(row.record_id != 1 for row in status.results)
    assert status.startup_active_ids == (1,)


@pytest.mark.parametrize("extra", [{"no_execute_code_local_ex": True}, {"focus_ex_error": True}, {"focus_ex_bad_type": True}])
def test_focus_uses_working_single_parameter_route(tmp_path, extra):
    run = _run(tmp_path, descriptor=_descriptor(), scenario={
        "target_process": "game.exe", "target_pid": 4321, "main_form_handle": 1024,
        "focus_available": True, **extra,
        "steps": [{"ticks": 120, "game_windows": GAME_WINDOWS, "iconic_windows": [],
                   "foreground_window": {"handle": 1024, "caption": "CE"}}],
    })
    status = run.status()
    assert status.focus_successes == 1
    assert status.focus_capability == "IsWindowVisible=executeCodeLocal; SetForegroundWindow=executeCodeLocal"
    assert "executeCodeLocalEx" in status.focus_error


def test_focus_reports_unusable_calls_without_claiming_no_window(tmp_path):
    run = _run(tmp_path, descriptor=_descriptor(), scenario={
        "target_process": "game.exe", "target_pid": 4321, "focus_available": True,
        "execute_code_local_error": True,
        "steps": [{"ticks": 120, "game_windows": GAME_WINDOWS}],
    })
    assert run.status().focus_reason == "call-error"
    assert "local call failed" in run.status().focus_error
    assert run.status().focus_successes == 0


def test_large_active_startup_keeps_only_one_proof(tmp_path):
    actions = tuple(StartupAction(i, "active", "1", (), True) for i in range(1, 2049))
    run = _run(tmp_path, descriptor=_descriptor(startup=actions), scenario={
        "target_process": "game.exe", "target_pid": 4321,
        "records": {i: {"active": False} for i in range(1, 2049)},
        "steps": [{"ticks": 120}],
    })
    status = run.status()
    assert status.startup_state == "applied"
    assert status.startup_active_ids == (1,)


@pytest.mark.parametrize("order", ["transition_first", "transition_last"])
def test_startup_proof_follows_the_leaf_that_actually_transitioned(tmp_path, order):
    # Two independent cheats, one of them already on. Which record can prove the
    # table works is not known before startup runs, so the one that was off and
    # came on is the one reported, whatever order the plan lists them in.
    transitions = StartupAction(1, "active", "1", ("Cheat A",), True)
    already_on = StartupAction(2, "active", "1", ("Cheat B",), True)
    actions = (transitions, already_on) if order == "transition_first" else (already_on, transitions)
    run = _run(tmp_path, descriptor=_descriptor(startup=actions), scenario={
        "target_process": "game.exe", "target_pid": 4321,
        "records": {1: {"active": False}, 2: {"active": True}},
        "steps": [{"ticks": 120}],
    })
    status = run.status()
    assert status.startup_state == "applied"
    assert status.startup_active_ids == (1,)


def test_enclosing_startup_script_alone_is_never_startup_proof(tmp_path):
    # The script a cheat needs switching on is machinery: only the cheat inside
    # it, which was already on here, could have proved anything.
    actions = (
        StartupAction(1, "active", "1", ("Script",), False),
        StartupAction(2, "active", "1", ("Script", "Cheat"), True),
    )
    run = _run(tmp_path, descriptor=_descriptor(startup=actions), scenario={
        "target_process": "game.exe", "target_pid": 4321,
        "records": {1: {"active": False}, 2: {"active": True}},
        "steps": [{"ticks": 120}],
    })
    status = run.status()
    assert status.startup_state == "applied"
    assert status.startup_active_ids == ()


@pytest.mark.parametrize("initial_active", [True, False])
@pytest.mark.parametrize("async_ticks", [0, 3])
def test_startup_proof_requires_observed_off_to_on_transition(tmp_path, initial_active, async_ticks):
    run = _run(tmp_path, descriptor=_descriptor(startup=(StartupAction(1, "active", "1", (), True),)), scenario={
        "target_process": "game.exe", "target_pid": 4321,
        "records": {1: {"active": initial_active, "activation_async_ticks": async_ticks}},
        "steps": [{"ticks": 120}],
    })
    assert run.status().startup_state == "applied"
    assert run.status().startup_active_ids == (() if initial_active else (1,))


# -- quiesce ---------------------------------------------------------------


def test_a_quiesce_puts_down_every_record_that_is_switched_on(tmp_path: Path):
    """The stop kills the process group, so `[DISABLE]` never runs on its own.

    What that leaves in the running game is the session's `jmp` patches and its
    allocation, which no later session can undo either: that block's restore
    reads symbols belonging to a Cheat Engine that no longer exists. Putting the
    records down first is what leaves the game as it was found.
    """
    descriptor = _descriptor()
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "record_order": [5, 6],
            "records": {
                5: {"active": True, "script": "[ENABLE]\nregistersymbol(x)\n"},
                6: {"active": True},
            },
            "steps": [{"ticks": 6}],
        },
        controls={"control.txt": _control(RuntimeCommand(1, "quiesce"))},
    )
    status = run.status()
    answered = [item for item in status.results if item.generation == 1]
    assert answered and answered[-1].ok is True
    assert "put_down=2" in (answered[-1].value or "")
    assert "unsettled=" in (answered[-1].value or "")


def test_a_quiesce_puts_the_records_inside_a_script_down_before_the_script(tmp_path: Path):
    """A child's bytes live inside the allocation its script made.

    Freeing that allocation first leaves every record inside it pointing at
    memory that is no longer there, so the enclosing Auto Assembler script is
    always last.
    """
    descriptor = _descriptor()
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            # Flat, the way Cheat Engine lists it, with the script first and the
            # records it owns after it: what decides the order is the tree each
            # record names through its own parent, not the order they are in.
            "record_order": [5, 6, 7],
            "records": {
                5: {"active": True, "script": "[ENABLE]\nalloc(mem,2048)\n"},
                6: {"active": True, "parent": 5},
                7: {"active": True, "parent": 5},
            },
            "steps": [{"ticks": 8}],
        },
        controls={"control.txt": _control(RuntimeCommand(1, "quiesce"))},
    )
    status = run.status()
    answered = [item for item in status.results if item.generation == 1]
    assert answered and answered[-1].ok is True
    assert "put_down=3" in (answered[-1].value or "")
    # The order is what this is about, and the stub records it.
    order = [line for line in run.stdout.splitlines() if line.startswith("deactivated ")]
    assert order == ["deactivated 6", "deactivated 7", "deactivated 5"]


def test_a_record_that_will_not_settle_is_reported_and_does_not_hang_the_stop(tmp_path: Path):
    """The bound is what makes running this unconditionally safe.

    A record that will not come down inside the same bound every other
    activation here uses is named, the walk carries on, and the stop that asked
    for this proceeds either way: it must never become less reliable than the
    stop that does none of this.
    """
    descriptor = _descriptor()
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "record_order": [5, 6],
            "records": {
                5: {"active": True, "restore_never_settles": True},
                6: {"active": True},
            },
            "steps": [{"ticks": 60}],
        },
        controls={"control.txt": _control(RuntimeCommand(1, "quiesce"))},
    )
    status = run.status()
    answered = [item for item in status.results if item.generation == 1]
    assert answered and answered[-1].ok is False
    assert answered[-1].error_code == "quiesce_unsettled"
    assert "unsettled=5" in (answered[-1].value or "")
    # The one that could come down still did.
    assert "put_down=1" in (answered[-1].value or "")


def test_a_quiesce_with_nothing_switched_on_says_so_and_costs_nothing(tmp_path: Path):
    descriptor = _descriptor()
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "record_order": [5],
            "records": {5: {"active": False}},
            "steps": [{"ticks": 4}],
        },
        controls={"control.txt": _control(RuntimeCommand(1, "quiesce"))},
    )
    answered = [item for item in run.status().results if item.generation == 1]
    assert answered and answered[-1].ok is True
    assert "put_down=0" in (answered[-1].value or "")


def test_a_quiesce_leaves_a_switch_at_its_off_value(tmp_path: Path):
    """Releasing a frozen record is not switching that cheat off.

    The record keeps the value it was frozen at, so a table of
    `0:Disabled/1:Enabled` flags would come down with every flag still at 1 in
    the running game: the panel would say nothing is active and the cheats would
    all still be running. The key each switch is off at comes from the same
    inspection the user reviewed, on the descriptor.
    """
    descriptor = _descriptor(switch_off=((6, "0"),))
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "record_order": [5, 6],
            "records": {
                5: {"active": True, "value": "7"},
                6: {"active": True, "value": "1"},
            },
            "steps": [{"ticks": 8}],
        },
        controls={"control.txt": _control(RuntimeCommand(1, "quiesce"))},
    )
    answered = [item for item in run.status().results if item.generation == 1]
    assert answered and answered[-1].ok is True
    assert "put_down=2" in (answered[-1].value or "")
    left = [line for line in run.stdout.splitlines() if line.startswith("value ")]
    # The switch is back at its off key; the record whose table declares none is
    # released and not written to, which is what a value nobody named means.
    assert left == ["value 5 7", "value 6 0"]


def test_a_switch_that_will_not_take_its_off_value_is_named_rather_than_counted(tmp_path: Path):
    """A cheat that is still running is what the user has to be told about.

    The record came down, so the count alone would report a clean stop while the
    value the game keeps is the one the cheat was frozen at.
    """
    descriptor = _descriptor(switch_off=((6, "0"),))
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "record_order": [6],
            "records": {6: {"active": True, "value": "1", "value_error": True}},
            "steps": [{"ticks": 8}],
        },
        controls={"control.txt": _control(RuntimeCommand(1, "quiesce"))},
    )
    answered = [item for item in run.status().results if item.generation == 1]
    assert answered and answered[-1].ok is False
    assert answered[-1].error_code == "quiesce_unsettled"
    assert "unsettled=6" in (answered[-1].value or "")
    assert "put_down=0" in (answered[-1].value or "")


def test_a_record_that_comes_on_while_the_quiesce_walks_is_still_put_down(tmp_path: Path):
    """The list a quiesce starts from is what was on when it started.

    A stop arriving moments after an auto-load leaves an activation Cheat Engine
    has not finished, and that record comes on behind the walk. It is waited for
    and then looked for again, because the stop kills Cheat Engine when this
    answers and a record switched on afterwards keeps its patch in the game.
    """
    descriptor = _descriptor(startup=(StartupAction(6, "active", "1", ("Cheat",), False),))
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "record_order": [5, 6],
            "records": {
                5: {"active": True},
                # Cheat Engine is still thinking about this one when the stop
                # arrives, so it is not in the first look at what is switched on.
                6: {"active": False, "activation_async_ticks": 3},
            },
            "steps": [{"ticks": 30}],
        },
        controls={"control.txt": _control(RuntimeCommand(1, "quiesce"))},
    )
    answered = [item for item in run.status().results if item.generation == 1]
    assert answered and answered[-1].ok is True
    assert "put_down=2" in (answered[-1].value or "")
    order = [line for line in run.stdout.splitlines() if line.startswith("deactivated ")]
    assert order == ["deactivated 5", "deactivated 6"]


def test_a_command_that_would_switch_a_cheat_on_during_a_quiesce_is_refused(tmp_path: Path):
    """Nothing may switch a cheat on while this session's are being switched off.

    The stop kills Cheat Engine when the quiesce answers, so a record activated
    behind the walk is one whose `[DISABLE]` never runs: its patch and its
    allocation stay in the running game and no later session can undo them.
    """
    descriptor = _descriptor()
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "record_order": [5, 6],
            "records": {5: {"active": True}, 6: {"active": False}},
            "steps": [{"ticks": 10}],
        },
        controls={"control.txt": _control(
            RuntimeCommand(1, "quiesce"),
            RuntimeCommand(2, "set_active", 6, "1"),
        )},
    )
    status = run.status()
    refused = [item for item in status.results if item.generation == 2]
    assert refused and refused[-1].ok is False
    assert refused[-1].error_code == "quiesce_in_progress"
    quiesced = [item for item in status.results if item.generation == 1]
    assert quiesced and "put_down=1" in (quiesced[-1].value or "")
    # And the record the command named never came on.
    assert [line for line in run.stdout.splitlines() if line.startswith("deactivated ")] == ["deactivated 5"]


def test_a_rollback_cannot_switch_a_cheat_on_after_the_quiesce_answered(tmp_path: Path):
    """A failed startup rolls itself back, and a rollback can switch a record on.

    It puts each record back the way it found it, so one that was on before
    startup switched it off comes back on. That may not happen after the stop
    has been told the game was put back: the stop kills Cheat Engine on that
    answer, and the record would keep whatever the table did to the game.
    """
    descriptor = _descriptor(startup=(
        StartupAction(7, "active", "0", ("Held",), False),
        StartupAction(8, "active", "1", ("Cheat",), False),
    ))
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "record_order": [7, 8],
            "records": {
                # On before startup, switched off by the plan: the rollback's
                # own snapshot therefore says this one was on.
                7: {"active": True},
                # The action that fails, which is what begins the rollback.
                8: {"active": False, "activation_never_settles": True},
            },
            "steps": [{"ticks": 30}],
        },
        controls={"control.txt": _control(RuntimeCommand(1, "quiesce"))},
    )
    answered = [item for item in run.status().results if item.generation == 1]
    assert answered and answered[-1].ok is True
    # The record the rollback switched back on was seen and put down, rather
    # than left on behind an answer that said the game was clean.
    assert "put_down=1" in (answered[-1].value or "")
    assert "active 7 false" in run.stdout.splitlines()


def test_a_quiesce_answers_inside_the_bound_the_stop_waits_under(tmp_path: Path):
    """The backend waits 15 seconds and then stops Cheat Engine regardless.

    Two records Cheat Engine never finishes thinking about would each take the
    per-record wait, and an answer after the stop is an answer nobody reads. The
    walk has a deadline of its own inside that bound, and what it had not
    reached is named rather than waited for.
    """
    descriptor = _descriptor()
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "record_order": [5, 6],
            "records": {
                5: {"active": True, "activation_async_never_settles": True},
                6: {"active": True, "activation_async_never_settles": True},
            },
            # Fewer ticks than two per-record waits would need, and more than
            # the walk's own deadline.
            "steps": [{"ticks": 55}],
        },
        controls={"control.txt": _control(RuntimeCommand(1, "quiesce"))},
    )
    answered = [item for item in run.status().results if item.generation == 1]
    assert answered and answered[-1].ok is False
    assert answered[-1].error_code == "quiesce_unsettled"
    value = answered[-1].value or ""
    assert "unsettled=5,6" in value and "put_down=0" in value


def test_work_that_never_settles_does_not_hold_the_answer_past_the_bound(tmp_path: Path):
    """The walk waits for what can still change the game, and not forever.

    A startup activation Cheat Engine never finishes with keeps this session
    able to switch a record on, so the walk cannot answer over it; the stop
    waits 15 seconds and no longer. The deadline is what turns that into an
    answer the stop can read, naming what nothing will now finish.
    """
    descriptor = _descriptor(startup=(StartupAction(6, "active", "1", ("Cheat",), False),))
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "record_order": [5, 6],
            "records": {
                5: {"active": True},
                6: {"active": False, "activation_async_never_settles": True},
            },
            "steps": [{"ticks": 70}],
        },
        controls={"control.txt": _control(RuntimeCommand(1, "quiesce"))},
    )
    answered = [item for item in run.status().results if item.generation == 1]
    assert answered and answered[-1].ok is False
    value = answered[-1].value or ""
    # The record that could come down did; what was still moving is named
    # rather than waited for.
    assert "put_down=1" in value
    assert "unsettled=" in value and value.split("unsettled=")[1] != ""


def test_an_activation_its_own_command_gave_up_on_is_still_the_stop_s_business(tmp_path: Path):
    """A command that times out says this session stopped watching.

    It does not say Cheat Engine stopped: the activation can still land, and a
    walk that passed the record while it was off would report a game nobody had
    put back. The record stays known until Cheat Engine is finished with it.
    """
    descriptor = _descriptor()
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "record_order": [6],
            # Longer than the command waits, shorter than the stop does, and
            # put down at once when the stop asks: an enable that compiles and
            # allocates, a disable that writes the original bytes back.
            "records": {6: {"active": False, "activation_async_ticks": 50, "disable_settles_at_once": True}},
            "steps": [{"ticks": 80}],
        },
        controls={"control.txt": _control(
            RuntimeCommand(1, "set_active", 6, "1"),
            RuntimeCommand(2, "quiesce"),
        )},
    )
    status = run.status()
    timed_out = [item for item in status.results if item.generation == 1]
    assert timed_out and timed_out[-1].ok is False
    assert timed_out[-1].error == "activation timed out"
    answered = [item for item in status.results if item.generation == 2]
    # It came on after the command gave up, and the stop still put it down.
    assert answered and answered[-1].ok is True
    assert "put_down=1" in (answered[-1].value or "")
    assert "active 6 false" in run.stdout.splitlines()


def test_an_activation_that_never_finishes_is_named_rather_than_forgotten(tmp_path: Path):
    """The other end of the same rule.

    Cheat Engine never finishes with the record, so the stop cannot say what
    the game was left holding. It says that, rather than reporting a clean
    stop with an operation still running inside a Cheat Engine about to be
    killed.
    """
    descriptor = _descriptor()
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "record_order": [6],
            "records": {6: {"active": False, "activation_async_never_settles": True}},
            "steps": [{"ticks": 120}],
        },
        controls={"control.txt": _control(
            RuntimeCommand(1, "set_active", 6, "1"),
            RuntimeCommand(2, "quiesce"),
        )},
    )
    answered = [item for item in run.status().results if item.generation == 2]
    assert answered and answered[-1].ok is False
    assert answered[-1].error_code == "quiesce_unsettled"
    assert "unsettled=6" in (answered[-1].value or "")


def test_a_table_that_keeps_switching_records_on_is_reported_not_declared_clean(tmp_path: Path):
    """The sweep allowance running out is not the game being clean.

    A table whose `[DISABLE]` switches the next record on is a real shape, and
    a walk that spent its allowance on that chain has one cheat still running.
    The stop finishes, bounded, and names it.
    """
    descriptor = _descriptor()
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "record_order": [1, 2, 3, 4, 5],
            "records": {
                1: {"active": True, "activates_on_disable": 2},
                2: {"active": False, "activates_on_disable": 3},
                3: {"active": False, "activates_on_disable": 4},
                4: {"active": False, "activates_on_disable": 5},
                5: {"active": False},
            },
            "steps": [{"ticks": 30}],
        },
        controls={"control.txt": _control(RuntimeCommand(1, "quiesce"))},
    )
    answered = [item for item in run.status().results if item.generation == 1]
    assert answered and answered[-1].ok is False
    value = answered[-1].value or ""
    assert "put_down=4" in value
    assert "unsettled=5" in value
    assert "active 5 true" in run.stdout.splitlines()


def test_a_startup_that_could_not_put_the_game_back_is_not_a_clean_stop(tmp_path: Path):
    """A failed startup changed the game before it failed.

    Its rollback is what puts that back, and a restore that was refused or read
    back wrong leaves a record holding whatever startup wrote. No walk will ever
    find it, because it is not switched on: the stop names it or reports a game
    as put back that nobody put back.
    """
    descriptor = _descriptor(startup=(
        StartupAction(7, "value", "5", ("Damage",), False),
        StartupAction(8, "active", "1", ("Cheat",), False),
    ))
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "record_order": [7, 8],
            "records": {
                # Whatever is written to it, it holds something else: the
                # startup write fails on read-back, and so does the restore.
                7: {"active": False, "value": "1", "clamp_value": "3"},
                8: {"active": False},
            },
            "steps": [{"ticks": 20}],
        },
        controls={"control.txt": _control(RuntimeCommand(1, "quiesce"))},
    )
    answered = [item for item in run.status().results if item.generation == 1]
    assert answered and answered[-1].ok is False
    assert answered[-1].error_code == "quiesce_unsettled"
    assert "unsettled=7" in (answered[-1].value or "")


def test_a_record_the_address_list_cannot_read_is_not_a_record_that_is_off(tmp_path: Path):
    """The walk's list is what it managed to ask about, not what the table holds.

    A record Cheat Engine will not answer for says nothing about itself, and
    counting it as `not active` is how a cheat leaves the stop's accounting: the
    answer would be a clean game with a record nobody looked at.
    """
    descriptor = _descriptor()
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "record_order": [5, 6],
            "records": {5: {"active": True}, 6: {"active": False}},
            # Unreadable before the quiesce is read from the control file.
            "steps": [{"unreadable_records": [6], "ticks": 12}],
        },
        controls={"control.txt": _control(RuntimeCommand(1, "quiesce"))},
    )
    answered = [item for item in run.status().results if item.generation == 1]
    assert answered and answered[-1].ok is False
    assert answered[-1].error_code == "quiesce_unsettled"
    value = answered[-1].value or ""
    # The one it could read came down; the one it could not is named rather
    # than counted as off.
    assert "put_down=1" in value
    assert "could not be read" in value


def test_a_record_that_will_not_say_which_one_it_is_is_not_counted_as_off(tmp_path: Path):
    """A table declares the key a cheat is switched off at by record ID.

    So a record whose own ID cannot be read has no key to be left at: releasing
    it leaves the value it was frozen at in the running game, and counting it as
    put down is the same leak the off keys exist to close, reported as clean.
    """
    descriptor = _descriptor(switch_off=((5, "0"), (6, "0")))
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "record_order": [5, 6],
            "records": {
                5: {"active": True, "value": "1"},
                6: {"active": True, "value": "1", "id_error": True},
            },
            "steps": [{"ticks": 8}],
        },
        controls={"control.txt": _control(RuntimeCommand(1, "quiesce"))},
    )
    answered = [item for item in run.status().results if item.generation == 1]
    assert answered and answered[-1].ok is False
    assert answered[-1].error_code == "quiesce_unsettled"
    value = answered[-1].value or ""
    assert "put_down=1" in value
    # Named by where it sits, because the number it would be named by is the
    # one thing it would not answer for.
    assert "the record at position 1" in value
    left = [line for line in run.stdout.splitlines() if line.startswith("value ")]
    # The one with an identity is left at its off key. The other was switched
    # off and never written to, which is exactly what the answer says.
    assert left == ["value 5 0", "value 6 1"]


def test_a_record_that_becomes_unreadable_mid_quiesce_is_not_already_off(tmp_path: Path):
    """It was switched on when the walk started, and nothing says it came down.

    A state that cannot be read reads the same as a child a script took down
    with it, and treating the two alike is how a cheat leaves the accounting:
    the stop kills the Cheat Engine that could have answered moments later.

    Nothing is written to it on the strength of that either. A record a
    script's `[DISABLE]` destroyed is one of the few things that reads this
    way, and the one thing that must not then be written to.
    """
    descriptor = _descriptor()
    run = _run(
        tmp_path,
        descriptor=descriptor,
        scenario={
            "target_process": "game.exe",
            "target_pid": 4321,
            "record_order": [5, 6],
            "records": {
                5: {"active": True},
                # Readable for the walk's own list, unreadable by the time the
                # walk reaches it.
                6: {"active": True, "active_reads_before_error": 1},
            },
            "steps": [{"ticks": 8}],
        },
        controls={"control.txt": _control(RuntimeCommand(1, "quiesce"))},
    )
    answered = [item for item in run.status().results if item.generation == 1]
    assert answered and answered[-1].ok is False
    assert answered[-1].error_code == "quiesce_unsettled"
    value = answered[-1].value or ""
    assert "put_down=1" in value
    assert "unsettled=6" in value
    assert "deactivated 6" not in run.stdout
