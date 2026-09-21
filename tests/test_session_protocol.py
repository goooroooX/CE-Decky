from pathlib import Path
import json
import os
import uuid

import pytest

from ce_decky.ct_inspector import inspect_table
from ce_decky.profiles import ProfileStore, StartupPreference
from ce_decky import session_protocol
from ce_decky.session_protocol import (
    RETAINED_SESSION_HISTORY,
    CONTROL_HEADER,
    PreparedSession,
    RuntimeCommand,
    RuntimeResult,
    RuntimeStatus,
    SessionDescriptor,
    SessionStore,
    StartupAction,
    parse_control,
    parse_descriptor,
    parse_status,
    percent_decode,
    percent_encode,
    render_control,
    render_descriptor,
    render_status,
    wine_z_path,
)
from ce_decky.table_store import TableStore

CE_SHA = "c" * 64
SESSION = "123e4567-e89b-42d3-a456-426614174000"

CT = b'''<CheatTable CheatEngineTableVersion="45"><CheatEntries>
<CheatEntry><ID>1</ID><Description>"Enable"</Description><GroupHeader>1</GroupHeader><CheatEntries>
<CheatEntry><ID>2</ID><Description>"Mode"</Description><VariableType>4 Bytes</VariableType><DropDownReadOnly>1</DropDownReadOnly><DropDownList>0:Off\n1:On</DropDownList></CheatEntry>
</CheatEntries></CheatEntry></CheatEntries></CheatTable>'''


def test_percent_codec_is_strict_and_roundtrips_unicode_and_delimiters():
    value = "hello\tworld % / Привет"
    encoded = percent_encode(value)
    assert "\t" not in encoded and " " not in encoded
    assert percent_decode(encoded) == value
    for bad in ["%", "%G0", "hello world", "é"]:
        with pytest.raises(ValueError):
            percent_decode(bad)


def test_descriptor_and_control_roundtrip_without_code_execution_surface():
    sid = str(uuid.uuid4())
    descriptor = SessionDescriptor(
        sid, 42, CE_SHA, "a" * 64, r"Z:\home\deck\table.CT", "game.exe",
        r"Z:\home\deck\control.txt", r"Z:\home\deck\status.txt",
        (StartupAction(2, "value", "1", ("Enable", "Mode")), StartupAction(2, "active", "1", ("Enable", "Mode"))),
    )
    raw = render_descriptor(descriptor)
    assert b"dofile" not in raw and b"load(" not in raw
    parsed = parse_descriptor(raw)
    assert parsed.session_id == sid
    assert [a.kind for a in parsed.startup] == ["value", "active"]

    commands = (
        RuntimeCommand(1, "query", 2),
        RuntimeCommand(2, "set_value", 2, "2\tTurbo"),
        RuntimeCommand(3, "set_active", 2, "1"),
        RuntimeCommand(4, "list_processes"),
    )
    assert parse_control(render_control(commands)) == commands


def test_session_prepare_rejects_legacy_startup_preferences_on_group_headers(tmp_path: Path):
    source = tmp_path / "group-startup.CT"
    source.write_bytes(CT)
    tables = TableStore(tmp_path / "tables")
    artifact = tables.import_ct(str(source))
    blob = tables.verified_blob(artifact.sha256)
    inspection = inspect_table(blob, artifact.sha256)

    profiles = ProfileStore(tmp_path / "profiles.json")
    profiles.upsert(app_id=41, name="Game", is_shortcut=False, table_sha256=artifact.sha256, target_process="game.exe")
    profiles.set_startup(app_id=41, table_sha256=artifact.sha256, record_id=1, active=True, value=None)
    profiles.set_execution_consent(app_id=41, table_sha256=artifact.sha256, consent=True)
    profile = profiles.get(41)
    assert profile is not None

    with pytest.raises(ValueError, match="group header"):
        SessionStore(tmp_path / "state", tmp_path).prepare(profile, blob, inspection, CE_SHA)


def test_protocol_rejects_duplicate_fields_and_noncanonical_numbers():
    sid = str(uuid.uuid4())
    good = render_descriptor(SessionDescriptor(sid, 1, CE_SHA, "a"*64, "Z:\\a", "game.exe", "Z:\\c", "Z:\\s", ()))
    duplicate = good + b"F\tapp_id\t1\n"
    with pytest.raises(ValueError, match="duplicate"):
        parse_descriptor(duplicate)
    bad_control = (CONTROL_HEADER + "\nC\t01\tquery\t1\t-\t-\n").encode()
    with pytest.raises(ValueError, match="canonical"):
        parse_control(bad_control)
    zero_control = (CONTROL_HEADER + "\nC\t0\tquery\t1\t-\t-\n").encode()
    with pytest.raises(ValueError, match="out of range"):
        parse_control(zero_control)


def test_session_prepare_binds_exact_sha_consent_and_orders_startup(tmp_path: Path):
    source = tmp_path / "game.CT"
    source.write_bytes(CT)
    tables = TableStore(tmp_path / "tables")
    artifact = tables.import_ct(str(source))
    blob = tables.verified_blob(artifact.sha256)
    inspection = inspect_table(blob, artifact.sha256)

    profiles = ProfileStore(tmp_path / "profiles.json")
    profiles.upsert(app_id=42, name="Game", is_shortcut=False, table_sha256=artifact.sha256, target_process="game.exe")
    profiles.set_startup(app_id=42, table_sha256=artifact.sha256, record_id=2, active=True, value="1")
    profile = profiles.get(42)
    assert profile is not None
    sessions = SessionStore(tmp_path / "state", tmp_path)
    with pytest.raises(ValueError, match="consent"):
        sessions.prepare(profile, blob, inspection, CE_SHA)

    profiles.set_execution_consent(app_id=42, table_sha256=artifact.sha256, consent=True)
    profile = profiles.get(42)
    assert profile is not None
    prepared = sessions.prepare(profile, blob, inspection, CE_SHA)
    parsed = parse_descriptor(Path(prepared.descriptor_path).read_bytes())
    assert [action.kind for action in parsed.startup] == ["value", "active"]
    assert parsed.table_sha256 == artifact.sha256
    assert parsed.table_path == wine_z_path(Path(prepared.descriptor_path).parent / "table.ct")
    assert len(prepared.descriptor_md5) == 32


def test_a_multi_line_table_load_error_does_not_make_the_status_unreadable():
    # Cheat Engine's own error text reaches this field, and a Lua error can carry
    # a traceback. Rejecting the whole status over its newline would report a
    # healthy attached session as corrupt protocol state - which is exactly the
    # failure this field exists to describe.
    status = RuntimeStatus(
        SESSION, 42, CE_SHA, "b" * 64, "d" * 64, 1234, True, "game.exe", 555, (), (), 0,
        table_load_state="failed", table_load_error="loadTable failed\nstack traceback:\n  [C]: in ?",
    )
    parsed = parse_status(render_status(status))
    assert parsed.table_load_state == "failed"
    assert "stack traceback" in (parsed.table_load_error or "")


def test_the_descriptor_states_whether_the_table_carries_a_lua_script():
    # The bridge cannot ask the table this question without reading it again,
    # and it is the fact that decides whether the route that would make Cheat
    # Engine ask about the script may be used at all. It comes from the same
    # inspection the user reviewed and consented to.
    descriptor = SessionDescriptor(
        SESSION, 42, CE_SHA, "b" * 64, "Z:\\s\\table.ct", "game.exe",
        "Z:\\s\\control.txt", "Z:\\s\\status.txt", (), False, True,
    )
    parsed = parse_descriptor(render_descriptor(descriptor))
    assert parsed.table_has_lua is True
    assert parse_descriptor(render_descriptor(SessionDescriptor(
        SESSION, 42, CE_SHA, "b" * 64, "Z:\\s\\table.ct", "game.exe",
        "Z:\\s\\control.txt", "Z:\\s\\status.txt", (), False, False,
    ))).table_has_lua is False


def test_the_descriptor_states_which_startup_records_could_prove_the_table_works():
    # Which record proves a table works is only known once startup has run, so
    # the descriptor states every record that would be eligible and the bridge
    # reports whichever of them actually came on.
    startup = (
        StartupAction(1, "active", "1", ("Script",), False),
        StartupAction(2, "active", "1", ("Script", "Cheat"), True),
        StartupAction(3, "active", "0", ("Off",), False),
    )
    descriptor = SessionDescriptor(
        SESSION, 42, CE_SHA, "b" * 64, "Z:\\s\\table.ct", "game.exe",
        "Z:\\s\\control.txt", "Z:\\s\\status.txt", startup, False, True,
    )
    rendered = render_descriptor(descriptor)
    assert [action.proof for action in parse_descriptor(rendered).startup] == [False, True, False]
    with pytest.raises(ValueError, match="proof eligibility"):
        parse_descriptor(rendered.replace(b"A\t3\tactive\t0\t0", b"A\t3\tactive\t0\t1"))
    with pytest.raises(ValueError, match="proof eligibility"):
        parse_descriptor(rendered.replace(b"A\t1\tactive\t1\t0", b"A\t1\tactive\t1\tyes"))
    # A session prepared before the column existed states no eligibility, and is
    # retired as stale rather than launched.
    legacy = b"\n".join(
        line[:-2] if line.startswith(b"A\t") else line for line in rendered.split(b"\n")
    )
    assert not any(action.proof for action in parse_descriptor(legacy).startup)


def test_a_descriptor_from_before_the_lua_script_field_still_parses():
    # A session prepared by an earlier build is still on disk after an update,
    # and "not stated" has to read as not stated rather than as a refusal.
    rendered = render_descriptor(SessionDescriptor(
        SESSION, 42, CE_SHA, "b" * 64, "Z:\\s\\table.ct", "game.exe",
        "Z:\\s\\control.txt", "Z:\\s\\status.txt", (), False, True,
    ))
    legacy = rendered.replace(b"F\ttable_has_lua\t1\n", b"")
    assert parse_descriptor(legacy).table_has_lua is None


def test_how_the_table_was_opened_and_whether_the_bridge_is_ready_survive_the_round_trip():
    # `approved` is the exact-SHA execution authorization from Review being
    # carried into Cheat Engine's own load, so the table's Lua script runs and
    # Cheat Engine never asks. A bug report about a table that ran code has to
    # be able to establish which of the two routes opened it.
    status = RuntimeStatus(
        SESSION, 42, CE_SHA, "b" * 64, "d" * 64, 1234, False, "game.exe", 0, (), (), 0,
        table_load_state="pending", table_load_route="approved", bridge_phase="starting",
    )
    parsed = parse_status(render_status(status))
    assert parsed.table_load_route == "approved"
    assert parsed.bridge_phase == "starting"

    prompted = parse_status(render_status(RuntimeStatus(
        SESSION, 42, CE_SHA, "b" * 64, "d" * 64, 1234, True, "game.exe", 555, (), (), 3,
        table_load_state="loaded", table_load_route="prompted", bridge_phase="ready",
    )))
    assert prompted.table_load_route == "prompted"
    assert prompted.bridge_phase == "ready"


def test_an_unknown_table_load_route_or_bridge_phase_is_refused():
    # Both are closed vocabularies the backend acts on: an unknown value must
    # fail the parse rather than be carried through as if it meant something.
    good = render_status(RuntimeStatus(
        SESSION, 42, CE_SHA, "b" * 64, "d" * 64, 1234, True, "game.exe", 555, (), (), 1,
        table_load_state="loaded", table_load_route="approved", bridge_phase="ready",
    ))
    with pytest.raises(ValueError):
        parse_status(good.replace(b"F\ttable_load_route\tapproved\n", b"F\ttable_load_route\tguessed\n"))
    with pytest.raises(ValueError):
        parse_status(good.replace(b"F\tbridge_phase\tready\n", b"F\tbridge_phase\tfinished\n"))


def test_a_bridge_that_predates_the_phase_field_reads_as_ready():
    # Every earlier bridge published nothing at all until it was ready, so an
    # absent phase is not an unknown one.
    status = RuntimeStatus(
        SESSION, 42, CE_SHA, "b" * 64, "d" * 64, 1234, True, "game.exe", 555, (), (), 1,
        table_load_state="loaded",
    )
    parsed = parse_status(render_status(status))
    assert parsed.bridge_phase is None
    assert parsed.table_load_route is None


def test_a_dismissed_dialog_survives_the_status_round_trip():
    # What Cheat Engine asked is arbitrary text from a table or from Cheat
    # Engine itself, and it is the whole point of the field: the user is being
    # told what was answered on their behalf.
    status = RuntimeStatus(
        SESSION, 42, CE_SHA, "b" * 64, "d" * 64, 1234, True, "game.exe", 555, (), (), 0,
        window_suppressions=0, dialogs_dismissed=2, last_dialog="Confirmation\tтаблица",
    )
    parsed = parse_status(render_status(status))
    assert parsed.dialogs_dismissed == 2
    assert parsed.last_dialog == "Confirmation\tтаблица"


def test_the_minimized_state_query_is_one_of_exactly_three_answers():
    """An unrecognised answer must not read as a working capability.

    A game is asked to come back only on a positive answer from this call, and
    the panel warns when there is no call to ask. A status carrying anything
    else would suppress that warning and describe a capability that is not
    there, so the value is checked rather than merely bounded.
    """
    for answer in ("user32.IsIconic", "IsIconic", "unavailable"):
        status = RuntimeStatus(
            SESSION, 42, CE_SHA, "b" * 64, "d" * 64, 1234, True, "game.exe", 555, (), (), 0,
            window_suppressions=0, minimized_query=answer,
        )
        assert parse_status(render_status(status)).minimized_query == answer

    healthy = render_status(RuntimeStatus(
        SESSION, 42, CE_SHA, "b" * 64, "d" * 64, 1234, True, "game.exe", 555, (), (), 0,
        window_suppressions=0, minimized_query="unavailable",
    ))
    forged = healthy.replace(b"unavailable", b"garbage")
    with pytest.raises(ValueError, match="minimized-state query is invalid"):
        parse_status(forged)


def test_the_half_that_failed_survives_the_status_round_trip():
    """Which call stopped a game being asked back, kept as a closed vocabulary.

    `minimized_query` reads `unavailable` both for a Cheat Engine that cannot
    say whether a window is minimized and for one that answers that perfectly
    well but cannot post the request, so it cannot be what a bug report is read
    from. This can, and only if an unrecognised value is refused rather than
    passed through as a reason nothing downstream knows how to describe.
    """
    for capability in ("ready", "no-local-call", "no-window", "iconic-unanswered", "iconic-disagrees", "no-post"):
        status = RuntimeStatus(
            SESSION, 42, CE_SHA, "b" * 64, "d" * 64, 1234, True, "game.exe", 555, (), (), 0,
            window_suppressions=0, restore_capability=capability,
        )
        assert parse_status(render_status(status)).restore_capability == capability

    # Rendered without a dash, because the renderer percent-encodes one and the
    # forgery has to replace the value the file actually carries.
    healthy = render_status(RuntimeStatus(
        SESSION, 42, CE_SHA, "b" * 64, "d" * 64, 1234, True, "game.exe", 555, (), (), 0,
        window_suppressions=0, restore_capability="ready",
    ))
    with pytest.raises(ValueError, match="restore capability is invalid"):
        parse_status(healthy.replace(b"ready", b"garbage"))


def test_a_bridge_that_reached_no_conclusion_says_nothing_about_the_capability():
    rendered = render_status(RuntimeStatus(
        SESSION, 42, CE_SHA, "b" * 64, "d" * 64, 1234, True, "game.exe", 555, (), (), 0,
        window_suppressions=0,
    ))
    assert b"restore_capability" not in rendered
    assert parse_status(rendered).restore_capability is None


def test_a_bridge_that_never_answered_the_minimized_question_says_nothing():
    rendered = render_status(RuntimeStatus(
        SESSION, 42, CE_SHA, "b" * 64, "d" * 64, 1234, True, "game.exe", 555, (), (), 0,
        window_suppressions=0,
    ))
    assert b"minimized_query" not in rendered
    assert parse_status(rendered).minimized_query is None


def test_a_bridge_that_met_no_dialog_says_nothing_about_dialogs():
    status = RuntimeStatus(
        SESSION, 42, CE_SHA, "b" * 64, "d" * 64, 1234, True, "game.exe", 555, (), (), 0,
        window_suppressions=0,
    )
    rendered = render_status(status)
    assert b"dialogs_dismissed" not in rendered
    parsed = parse_status(rendered)
    assert parsed.dialogs_dismissed is None and parsed.last_dialog is None


def test_status_from_an_earlier_bridge_still_parses(tmp_path: Path):
    # A Cheat Engine that outlived a plugin update keeps writing the diagnostics
    # its own bridge knows. Rejecting the whole status over one retired field
    # turned a healthy attached session into "runtime protocol state is
    # unreadable" - which reads as corruption and takes live control with it.
    status = RuntimeStatus(
        str(uuid.uuid4()), 42, CE_SHA, "b" * 64, "d" * 64, 1234, True, "game.exe", 555, (), (), 17,
    )
    legacy = render_status(status).replace(
        b"F\taddress_list_count\t17\n",
        b"F\taddress_list_count\t17\nF\ttable_path_matches\t1\n",
    )
    parsed = parse_status(legacy)
    assert parsed.address_list_count == 17
    # The retired field is accepted, never modelled: this bridge said nothing
    # about whether it got the table in, so neither does the parse.
    assert parsed.table_load_state is None


def test_session_status_must_match_descriptor_identity(tmp_path: Path):
    source = tmp_path / "game.CT"
    source.write_bytes(CT)
    tables = TableStore(tmp_path / "tables")
    artifact = tables.import_ct(str(source))
    blob = tables.verified_blob(artifact.sha256)
    inspection = inspect_table(blob, artifact.sha256)
    profiles = ProfileStore(tmp_path / "profiles.json")
    profiles.upsert(app_id=42, name="Game", is_shortcut=False, table_sha256=artifact.sha256, target_process="game.exe")
    profiles.set_execution_consent(app_id=42, table_sha256=artifact.sha256, consent=True)
    store = SessionStore(tmp_path / "state", tmp_path)
    prepared = store.prepare(profiles.get(42), blob, inspection, CE_SHA)  # type: ignore[arg-type]
    store.write_commands(prepared, [RuntimeCommand(1, "query", 2)])
    status = RuntimeStatus(
        prepared.session_id, 42, CE_SHA, artifact.sha256, prepared.descriptor_sha256, 1234, True, "game.exe", 555,
        (RuntimeResult(1, 2, True, True, "1", None),), ((555, "game.exe"),), 17, "loaded",
    )
    Path(prepared.status_path).write_bytes(render_status(status))
    parsed = store.read_status(prepared)
    assert parsed is not None and parsed.attached and parsed.opened_process_id == 555
    assert parsed.address_list_count == 17
    assert parsed.table_load_state == "loaded"

    typed = RuntimeStatus(
        prepared.session_id, 42, CE_SHA, artifact.sha256, prepared.descriptor_sha256, 1234,
        True, "game.exe", 555,
        (RuntimeResult(1, 2, False, None, None, "MemoryRecord missing", "record_missing"),), (),
    )
    typed_raw = render_status(typed)
    assert parse_status(typed_raw).results[0].error_code == "record_missing"
    # A surviving bridge from before the typed-result contract remains readable,
    # but its untyped failure can no longer be mistaken for expected absence.
    legacy_raw = b"\n".join(
        line.rsplit(b"\t", 1)[0] if line.startswith(b"R\t") else line
        for line in typed_raw.split(b"\n")
    )
    assert parse_status(legacy_raw).results[0].error_code is None

    wrong = RuntimeStatus(
        prepared.session_id, 42, CE_SHA, artifact.sha256, "b"*64, 1234, True, "game.exe", 555, (), (),
    )
    Path(prepared.status_path).write_bytes(render_status(wrong))
    with pytest.raises(ValueError, match="descriptor SHA"):
        store.read_status(prepared)

    wrong_ce = RuntimeStatus(
        prepared.session_id, 42, "d"*64, artifact.sha256, prepared.descriptor_sha256, 1234, True, "game.exe", 555, (), (),
    )
    Path(prepared.status_path).write_bytes(render_status(wrong_ce))
    with pytest.raises(ValueError, match="CE SHA"):
        store.read_status(prepared)


def test_session_descriptor_tamper_is_detected_before_commands(tmp_path: Path):
    # Construct a prepared record and descriptor directly to focus on exact descriptor hashing.
    sid = str(uuid.uuid4())
    store = SessionStore(tmp_path / "state", tmp_path)
    root = store.root / "1" / sid
    root.mkdir(parents=True)
    descriptor = root / "descriptor.txt"
    raw = render_descriptor(SessionDescriptor(sid, 1, CE_SHA, "a"*64, "Z:\\t", "g.exe", "Z:\\c", "Z:\\s", ()))
    descriptor.write_bytes(raw)
    import hashlib
    prepared = PreparedSession(sid, 1, CE_SHA, "a"*64, str(descriptor), hashlib.sha256(raw).hexdigest(), str(root/"control.txt"), str(root/"status.txt"), wine_z_path(descriptor), hashlib.md5(raw).hexdigest())
    descriptor.write_bytes(raw.replace(b"g.exe", b"x.exe"))
    with pytest.raises(ValueError, match="SHA-256"):
        store.write_commands(prepared, [RuntimeCommand(1, "query", 1)])


def test_runtime_command_log_survives_frontend_reload_and_preserves_unacked_commands(tmp_path: Path):
    source = tmp_path / "game.CT"
    source.write_bytes(CT)
    tables = TableStore(tmp_path / "tables")
    artifact = tables.import_ct(str(source))
    blob = tables.verified_blob(artifact.sha256)
    inspection = inspect_table(blob, artifact.sha256)
    profiles = ProfileStore(tmp_path / "profiles.json")
    profiles.upsert(app_id=42, name="Game", is_shortcut=False, table_sha256=artifact.sha256, target_process="game.exe")
    profiles.set_execution_consent(app_id=42, table_sha256=artifact.sha256, consent=True)
    profile = profiles.get(42)
    assert profile is not None
    store = SessionStore(tmp_path / "state", tmp_path)
    prepared = store.prepare(profile, blob, inspection, CE_SHA)

    assert store.next_generation(prepared) == 1
    assert store.write_commands(prepared, [RuntimeCommand(1, "list_processes")]) == 2
    assert store.next_generation(prepared) == 2
    assert store.write_commands(prepared, [RuntimeCommand(2, "query", 2)]) == 3
    assert [item.generation for item in parse_control(Path(prepared.control_path).read_bytes())] == [1, 2]

    with pytest.raises(ValueError, match="stale"):
        store.write_commands(prepared, [RuntimeCommand(2, "query", 2)])

    status = RuntimeStatus(
        prepared.session_id, 42, CE_SHA, artifact.sha256, prepared.descriptor_sha256, 1234, True, "game.exe", 555,
        (RuntimeResult(0, None, True, None, None, None), RuntimeResult(1, None, True, None, None, None), RuntimeResult(2, 2, True, False, "0", None)), (),
    )
    Path(prepared.status_path).write_bytes(render_status(status))
    assert store.next_generation(prepared) == 3
    assert store.write_commands(prepared, [RuntimeCommand(3, "set_active", 2, "1")]) == 4
    # Acknowledged generations are pruned; only the new pending command remains.
    assert [item.generation for item in parse_control(Path(prepared.control_path).read_bytes())] == [3]


def test_runtime_status_cannot_ack_a_generation_that_was_never_issued(tmp_path: Path):
    store, prepared, artifact, *_ = _prepared_session_fixture(tmp_path)
    assert store.write_commands(prepared, [RuntimeCommand(1, "list_processes")]) == 2
    forged = RuntimeStatus(
        prepared.session_id, prepared.app_id, prepared.ce_sha256, artifact.sha256, prepared.descriptor_sha256,
        1234, True, "game.exe", 555, (RuntimeResult(99, None, True, None, None, None),), (),
    )
    Path(prepared.status_path).write_bytes(render_status(forged))
    with pytest.raises(ValueError, match="absent from the control log"):
        store.next_generation(prepared)
    with pytest.raises(ValueError, match="absent from the control log"):
        store.write_commands(prepared, [RuntimeCommand(2, "query", 2)])


def test_runtime_status_rejects_duplicate_positive_result_generations(tmp_path: Path):
    store, prepared, artifact, *_ = _prepared_session_fixture(tmp_path)
    store.write_commands(prepared, [RuntimeCommand(1, "query", 2), RuntimeCommand(2, "query", 2)])
    valid = RuntimeStatus(
        prepared.session_id, prepared.app_id, prepared.ce_sha256, artifact.sha256, prepared.descriptor_sha256,
        1234, True, "game.exe", 555,
        (RuntimeResult(1, 2, True, False, "0", None), RuntimeResult(2, 2, True, False, "0", None)), (),
    )
    forged = render_status(valid).replace(b"R\t2\t2\t", b"R\t1\t2\t", 1)
    with pytest.raises(ValueError, match="duplicate runtime result generation"):
        parse_status(forged)


def test_runtime_status_result_identity_must_match_unpruned_control_command(tmp_path: Path):
    store, prepared, artifact, *_ = _prepared_session_fixture(tmp_path)
    store.write_commands(prepared, [RuntimeCommand(1, "query", 2)])
    forged = RuntimeStatus(
        prepared.session_id, prepared.app_id, prepared.ce_sha256, artifact.sha256, prepared.descriptor_sha256,
        1234, True, "game.exe", 555, (RuntimeResult(1, 1, True, False, "0", None),), (),
    )
    Path(prepared.status_path).write_bytes(render_status(forged))
    with pytest.raises(ValueError, match="MemoryRecord identity"):
        store.read_status(prepared)


def test_session_descriptor_md5_integrity_guard_is_independent_of_host_sha(tmp_path: Path):
    sid = str(uuid.uuid4())
    store = SessionStore(tmp_path / "state", tmp_path)
    root = store.root / "1" / sid
    root.mkdir(parents=True)
    descriptor = root / "descriptor.txt"
    raw = render_descriptor(SessionDescriptor(sid, 1, CE_SHA, "a"*64, "Z:\\t", "g.exe", "Z:\\c", "Z:\\s", ()))
    descriptor.write_bytes(raw)
    import hashlib
    prepared = PreparedSession(
        sid, 1, CE_SHA, "a"*64, str(descriptor), hashlib.sha256(raw).hexdigest(),
        str(root/"control.txt"), str(root/"status.txt"), wine_z_path(descriptor), "0"*32,
    )
    with pytest.raises(ValueError, match="MD5 integrity"):
        store.write_commands(prepared, [RuntimeCommand(1, "query", 1)])


def _prepared_session_fixture(tmp_path: Path, *, ct: bytes = CT):
    source = tmp_path / "game.CT"
    source.write_bytes(ct)
    tables = TableStore(tmp_path / "tables")
    artifact = tables.import_ct(str(source))
    blob = tables.verified_blob(artifact.sha256)
    inspection = inspect_table(blob, artifact.sha256)
    profiles = ProfileStore(tmp_path / "profiles.json")
    profiles.upsert(app_id=42, name="Game", is_shortcut=False, table_sha256=artifact.sha256, target_process="game.exe")
    profiles.set_execution_consent(app_id=42, table_sha256=artifact.sha256, consent=True)
    profile = profiles.get(42)
    assert profile is not None
    store = SessionStore(tmp_path / "state", tmp_path)
    return store, store.prepare(profile, blob, inspection, CE_SHA), artifact, blob, inspection, profiles


def test_session_rejects_symlinked_session_directory_after_prepare(tmp_path: Path):
    import shutil
    store, prepared, *_ = _prepared_session_fixture(tmp_path)
    session_root = Path(prepared.descriptor_path).parent
    outside = tmp_path / "outside-session"
    shutil.move(str(session_root), str(outside))
    session_root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        store.load_current(42)


def test_session_rejects_semantically_wrong_descriptor_paths_even_with_matching_hashes(tmp_path: Path):
    import hashlib
    store, prepared, *_ = _prepared_session_fixture(tmp_path)
    descriptor_path = Path(prepared.descriptor_path)
    parsed = parse_descriptor(descriptor_path.read_bytes())
    wrong = SessionDescriptor(
        parsed.session_id, parsed.app_id, parsed.ce_sha256, parsed.table_sha256,
        parsed.table_path, parsed.target_process, r"Z:\elsewhere\control.txt", parsed.status_path, parsed.startup,
    )
    raw = render_descriptor(wrong)
    descriptor_path.write_bytes(raw)
    changed = PreparedSession(
        prepared.session_id, prepared.app_id, prepared.ce_sha256, prepared.table_sha256,
        prepared.descriptor_path, hashlib.sha256(raw).hexdigest(), prepared.control_path,
        prepared.status_path, prepared.descriptor_windows_path, hashlib.md5(raw).hexdigest(), prepared.is_shortcut,
    )
    with pytest.raises(ValueError, match="control path mismatch"):
        store.write_commands(changed, [RuntimeCommand(1, "query", 2)])


def test_status_parser_rejects_excessive_result_and_process_rows():
    sid = str(uuid.uuid4())
    base = RuntimeStatus(sid, 1, CE_SHA, "a" * 64, "b" * 64, 1, False, "g.exe", 0, (), ())
    many_results = RuntimeStatus(
        **{**base.__dict__, "results": tuple(RuntimeResult(i, 1, True, False, "0", None) for i in range(257))}
    )
    with pytest.raises(ValueError, match="too many runtime results"):
        parse_status(render_status(many_results))
    many_processes = RuntimeStatus(
        **{**base.__dict__, "processes": tuple((i + 1, f"p{i}.exe") for i in range(1025))}
    )
    with pytest.raises(ValueError, match="too many process rows"):
        parse_status(render_status(many_processes))


def test_startup_rejects_duplicate_memory_record_ids_in_exact_table(tmp_path: Path):
    duplicate = b'''<CheatTable><CheatEntries>
    <CheatEntry><ID>2</ID><Description>"A"</Description></CheatEntry>
    <CheatEntry><ID>2</ID><Description>"B"</Description></CheatEntry>
    </CheatEntries></CheatTable>'''
    source = tmp_path / "dup.CT"
    source.write_bytes(duplicate)
    tables = TableStore(tmp_path / "tables")
    artifact = tables.import_ct(str(source))
    blob = tables.verified_blob(artifact.sha256)
    inspection = inspect_table(blob, artifact.sha256)
    profiles = ProfileStore(tmp_path / "profiles.json")
    profiles.upsert(app_id=42, name="Game", is_shortcut=False, table_sha256=artifact.sha256, target_process="game.exe")
    profiles.set_startup(app_id=42, table_sha256=artifact.sha256, record_id=2, active=True, value=None)
    profiles.set_execution_consent(app_id=42, table_sha256=artifact.sha256, consent=True)
    profile = profiles.get(42)
    assert profile is not None
    with pytest.raises(ValueError, match="ambiguous"):
        SessionStore(tmp_path / "state", tmp_path).prepare(profile, blob, inspection, CE_SHA)


def test_protocol_rejects_lone_unicode_surrogate_as_validation_error():
    with pytest.raises(ValueError, match="valid Unicode"):
        percent_encode("bad\ud800")


def test_control_contract_rejects_semantically_invalid_field_combinations():
    with pytest.raises(ValueError, match="set_value requires"):
        render_control((RuntimeCommand(1, "set_value", 2, None),))
    with pytest.raises(ValueError, match="does not accept a value"):
        render_control((RuntimeCommand(1, "query", 2, "unexpected"),))
    with pytest.raises(ValueError, match="does not accept a MemoryRecord ID"):
        render_control((RuntimeCommand(1, "list_processes", 2, None),))
    with pytest.raises(ValueError, match="basename"):
        render_control((RuntimeCommand(1, "retry_attach", None, "../game.exe"),))

    raw = (CONTROL_HEADER + "\nC\t1\tset_value\t2\t-\t-\n").encode()
    with pytest.raises(ValueError, match="set_value requires"):
        parse_control(raw)


def test_nullable_protocol_fields_round_trip_a_literal_hyphen():
    command = RuntimeCommand(1, "set_value", 2, "-")
    rendered = render_control((command,))
    assert b"%2D" in rendered
    assert parse_control(rendered) == (command,)
    status = RuntimeStatus(
        session_id="12345678-1234-4234-9234-123456789abc", app_id=10,
        ce_sha256="1" * 64, table_sha256="2" * 64, descriptor_sha256="3" * 64,
        heartbeat_ms=1, attached=False, target_process="game.exe", opened_process_id=0,
        results=(RuntimeResult(1, 2, False, None, "-", "-"),), processes=(),
    )
    parsed = parse_status(render_status(status))
    assert parsed.results[0].value == "-"
    assert parsed.results[0].error == "-"


def test_exact_pid_retry_attach_requires_its_expected_basename():
    command = RuntimeCommand(1, "retry_attach", value="game.exe", target_pid=42)
    assert parse_control(render_control((command,))) == (command,)
    with pytest.raises(ValueError, match="requires an expected process basename"):
        render_control((RuntimeCommand(1, "retry_attach", target_pid=42),))


def test_descriptor_contract_rejects_non_wine_paths_and_invalid_target_process():
    sid = str(uuid.uuid4())
    with pytest.raises(ValueError, match="Wine Z"):
        render_descriptor(SessionDescriptor(sid, 1, CE_SHA, "a" * 64, "/tmp/table.CT", "game.exe", "Z:\\c", "Z:\\s", ()))
    with pytest.raises(ValueError, match="basename"):
        render_descriptor(SessionDescriptor(sid, 1, CE_SHA, "a" * 64, "Z:\\t", "dir/game.exe", "Z:\\c", "Z:\\s", ()))
    with pytest.raises(ValueError, match="startup active"):
        render_descriptor(SessionDescriptor(sid, 1, CE_SHA, "a" * 64, "Z:\\t", "game.exe", "Z:\\c", "Z:\\s", (StartupAction(1, "active", "yes", ()),)))


def test_descriptor_renderer_rejects_duplicate_startup_actions(tmp_path):
    from ce_decky.session_protocol import SessionDescriptor, StartupAction, render_descriptor
    descriptor = SessionDescriptor(
        session_id="12345678-1234-4234-9234-123456789abc",
        app_id=10,
        ce_sha256="1" * 64,
        table_sha256="2" * 64,
        table_path=r"Z:\tmp\x.ct",
        target_process="game.exe",
        control_path=r"Z:\tmp\control.txt",
        status_path=r"Z:\tmp\status.txt",
        startup=(StartupAction(1, "active", "1", ()), StartupAction(1, "active", "0", ())),
    )
    with pytest.raises(ValueError, match="duplicate startup"):
        render_descriptor(descriptor)


def test_control_parser_enforces_command_count(monkeypatch):
    import ce_decky.session_protocol as protocol
    monkeypatch.setattr(protocol, "MAX_COMMANDS", 2)
    payload = (
        protocol.CONTROL_HEADER + "\n"
        "C\t1\tquery\t1\t-\t-\n"
        "C\t2\tquery\t1\t-\t-\n"
        "C\t3\tquery\t1\t-\t-\n"
    ).encode()
    with pytest.raises(ValueError, match="too many runtime commands"):
        protocol.parse_control(payload)


def test_status_renderer_enforces_per_field_bounds(monkeypatch):
    import ce_decky.session_protocol as protocol
    monkeypatch.setattr(protocol, "MAX_STATUS_TEXT_BYTES", 4)
    status = protocol.RuntimeStatus(
        session_id="12345678-1234-4234-9234-123456789abc", app_id=10,
        ce_sha256="1" * 64, table_sha256="2" * 64, descriptor_sha256="3" * 64,
        heartbeat_ms=1, attached=False, target_process="game.exe", opened_process_id=0,
        results=(protocol.RuntimeResult(1, 1, True, False, "12345", None),), processes=(),
    )
    with pytest.raises(ValueError, match="result value is too long"):
        protocol.render_status(status)


def test_runtime_command_value_limit_is_symmetric_for_render_and_parse():
    from ce_decky.session_protocol import MAX_RUNTIME_COMMAND_VALUE_BYTES
    huge = "x" * (MAX_RUNTIME_COMMAND_VALUE_BYTES + 1)
    with pytest.raises(ValueError, match="too long"):
        render_control((RuntimeCommand(1, "set_value", 1, huge),))
    encoded = percent_encode(huge)
    with pytest.raises(ValueError, match="too long"):
        parse_control((CONTROL_HEADER + f"\nC\t1\tset_value\t1\t{encoded}\t-\n").encode("utf-8"))


def test_dangling_status_symlink_is_corruption_not_silently_missing(tmp_path: Path):
    store, prepared, *_ = _prepared_session_fixture(tmp_path)
    status = Path(prepared.status_path)
    status.symlink_to(tmp_path / "missing-status")
    with pytest.raises(ValueError, match="status path"):
        store.next_generation(prepared)



def test_session_inventory_is_bounded_and_reports_corrupt_entries(tmp_path):
    from ce_decky.session_protocol import SessionStore
    store = SessionStore(tmp_path / "state", tmp_path)
    assert store.inventory() == {"apps": [], "total_sessions": 0, "errors": []}
    app = store.root / "10"
    app.mkdir(parents=True)
    (app / "not-a-session").mkdir()
    (app / "junk.txt").write_text("x")
    inventory = store.inventory()
    assert inventory["apps"][0]["app_id"] == 10
    assert inventory["apps"][0]["session_count"] == 0
    assert inventory["apps"][0]["corrupt_entries"] == 2


def test_session_inventory_rejects_symlink_root(tmp_path):
    from ce_decky.session_protocol import SessionStore
    outside = tmp_path / "outside"
    outside.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    (state / "sessions").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="session root"):
        SessionStore(state, tmp_path).inventory()


def test_session_inventory_applies_bounds_before_materializing_directory(monkeypatch, tmp_path):
    import ce_decky.session_protocol as protocol

    store = protocol.SessionStore(tmp_path / "state", tmp_path)
    store.root.mkdir(parents=True)
    (store.root / "1").mkdir()
    (store.root / "2").mkdir()
    monkeypatch.setattr(protocol, "MAX_SESSION_INVENTORY_APPS", 1)
    with pytest.raises(ValueError, match="app-directory limit"):
        store.inventory()


def test_session_inventory_bounds_every_per_app_entry(monkeypatch, tmp_path):
    import ce_decky.session_protocol as protocol

    store = protocol.SessionStore(tmp_path / "state", tmp_path)
    app = store.root / "10"
    app.mkdir(parents=True)
    (app / "junk-a").write_text("a", encoding="utf-8")
    (app / "junk-b").write_text("b", encoding="utf-8")
    monkeypatch.setattr(protocol, "MAX_SESSION_INVENTORY_APP_ENTRIES", 1)
    with pytest.raises(ValueError, match="per-app entry limit"):
        store.inventory()


def test_runtime_target_switch_is_authorized_by_retained_retry_attach_command(tmp_path: Path):
    store, prepared, artifact, *_ = _prepared_session_fixture(tmp_path)
    assert store.write_commands(prepared, [RuntimeCommand(1, "retry_attach", None, "alternate.exe")]) == 2
    switched = RuntimeStatus(
        prepared.session_id, prepared.app_id, prepared.ce_sha256, artifact.sha256, prepared.descriptor_sha256,
        100, True, "alternate.exe", 222, (RuntimeResult(1, None, True, None, None, None),), (),
    )
    Path(prepared.status_path).write_bytes(render_status(switched))
    assert store.read_status(prepared).target_process == "alternate.exe"

    assert store.write_commands(prepared, [RuntimeCommand(2, "query", 2)]) == 3
    assert [(c.generation, c.kind, c.value) for c in parse_control(Path(prepared.control_path).read_bytes())] == [
        (1, "retry_attach", "alternate.exe"), (2, "query", None),
    ]
    switched2 = RuntimeStatus(
        prepared.session_id, prepared.app_id, prepared.ce_sha256, artifact.sha256, prepared.descriptor_sha256,
        200, True, "alternate.exe", 222,
        (RuntimeResult(1, None, True, None, None, None), RuntimeResult(2, 2, True, False, "0", None)), (),
    )
    Path(prepared.status_path).write_bytes(render_status(switched2))
    assert store.write_commands(prepared, [RuntimeCommand(3, "query", 2)]) == 4
    # The acknowledged query is pruned but target authorization remains.
    assert [(c.generation, c.kind) for c in parse_control(Path(prepared.control_path).read_bytes())] == [(1, "retry_attach"), (3, "query")]


def test_a_session_names_every_executable_it_has_been_pointed_at(tmp_path: Path):
    """A stop has to prove the game this session changed is gone, not a game.

    The descriptor names the executable the session started with, and a retried
    attach names another. What was patched before that retry is still in the
    program the bridge moved away from, and that program goes on running with
    no bridge in it, so both names are the question and neither is the answer
    on its own.
    """
    store, prepared, *_ = _prepared_session_fixture(tmp_path)
    assert store.session_targets(prepared) == ("game.exe",)

    store.write_commands(prepared, [RuntimeCommand(1, "retry_attach", None, "alternate.exe")])
    assert store.session_targets(prepared) == ("game.exe", "alternate.exe")

    # A retry back to where it started adds no second name.
    store.write_commands(prepared, [RuntimeCommand(2, "retry_attach", None, "game.exe")])
    assert store.session_targets(prepared) == ("game.exe", "alternate.exe")


def test_a_session_says_which_executable_it_is_pointed_at_now(tmp_path: Path):
    """What the launcher watches to know the game is still there.

    Not the same question as which executables may have been left changed: a
    retried attach moves the session, and the program it moved away from goes
    on running without it. Watching the name the launch started with stops
    Cheat Engine when that program exits, in the middle of the game.
    """
    store, prepared, *_ = _prepared_session_fixture(tmp_path)
    assert store.session_target(prepared) == "game.exe"

    store.write_commands(prepared, [RuntimeCommand(1, "retry_attach", None, "alternate.exe")])
    assert store.session_target(prepared) == "alternate.exe"

    # And back, which `session_targets` collapses and this must not.
    store.write_commands(prepared, [RuntimeCommand(2, "retry_attach", None, "game.exe")])
    assert store.session_target(prepared) == "game.exe"


def test_runtime_status_cannot_claim_uncommanded_target_process(tmp_path: Path):
    store, prepared, artifact, *_ = _prepared_session_fixture(tmp_path)
    forged = RuntimeStatus(
        prepared.session_id, prepared.app_id, prepared.ce_sha256, artifact.sha256, prepared.descriptor_sha256,
        100, True, "other.exe", 333, (), (),
    )
    Path(prepared.status_path).write_bytes(render_status(forged))
    with pytest.raises(ValueError, match="target process mismatch"):
        store.read_status(prepared)


def test_session_executes_private_exact_sha_table_snapshot_and_rejects_drift(tmp_path: Path):
    source = tmp_path / "game.CT"
    source.write_bytes(CT)
    tables = TableStore(tmp_path / "tables")
    artifact = tables.import_ct(str(source))
    blob = tables.verified_blob(artifact.sha256)
    inspection = inspect_table(blob, artifact.sha256)
    profiles = ProfileStore(tmp_path / "profiles.json")
    profiles.upsert(app_id=42, name="Game", is_shortcut=False, table_sha256=artifact.sha256, target_process="game.exe")
    profiles.set_execution_consent(app_id=42, table_sha256=artifact.sha256, consent=True)
    store = SessionStore(tmp_path / "state", tmp_path)
    prepared = store.prepare(profiles.get(42), blob, inspection, CE_SHA)  # type: ignore[arg-type]
    session_table = Path(prepared.descriptor_path).parent / "table.ct"
    assert session_table.read_bytes() == CT
    assert parse_descriptor(Path(prepared.descriptor_path).read_bytes()).table_path == wine_z_path(session_table)

    # Later corruption of the shared table store must not change this prepared session.
    blob.chmod(0o600)
    blob.write_bytes(CT.replace(b"Mode", b"M0de"))
    assert session_table.read_bytes() == CT
    assert store.load_current(42) is not None

    # Corruption of the session-local execution snapshot itself is fail-closed.
    session_table.chmod(0o600)
    session_table.write_bytes(CT.replace(b"Mode", b"M0de"))
    with pytest.raises(ValueError, match="table snapshot failed exact SHA-256"):
        store.load_current(42)


def test_runtime_status_cannot_revert_target_after_acknowledged_retry_attach(tmp_path: Path):
    store, prepared, artifact, *_ = _prepared_session_fixture(tmp_path)
    store.write_commands(prepared, [RuntimeCommand(1, "retry_attach", None, "alternate.exe")])
    descriptor = parse_descriptor(Path(prepared.descriptor_path).read_bytes())
    stale_target = RuntimeStatus(
        session_id=prepared.session_id,
        app_id=prepared.app_id,
        ce_sha256=prepared.ce_sha256,
        table_sha256=prepared.table_sha256,
        descriptor_sha256=prepared.descriptor_sha256,
        heartbeat_ms=1,
        attached=False,
        target_process=descriptor.target_process,
        opened_process_id=0,
        results=(RuntimeResult(1, None, False, None, None, "attach failed"),),
        processes=(),
    )
    Path(prepared.status_path).write_bytes(render_status(stale_target))
    with pytest.raises(ValueError, match="target process mismatch"):
        store.read_status(prepared)


def test_session_pointer_and_metadata_reject_boolean_schema(tmp_path: Path):
    store, prepared, *_ = _prepared_session_fixture(tmp_path)
    app_root = store.root / str(prepared.app_id)
    current = app_root / "current.json"
    current.write_text('{"schema":true,"session_id":"' + prepared.session_id + '"}', encoding="utf-8")
    with pytest.raises(ValueError, match="pointer is corrupt"):
        store.load_current(prepared.app_id)

    # Restore pointer, then corrupt only session metadata schema.
    current.write_text('{"schema":2,"session_id":"' + prepared.session_id + '"}', encoding="utf-8")
    metadata = app_root / prepared.session_id / "session.json"
    import json
    raw = json.loads(metadata.read_text(encoding="utf-8"))
    raw["schema"] = True
    metadata.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="metadata is corrupt"):
        store.load_current(prepared.app_id)


def test_status_process_rows_reject_display_control_spoofing():
    status = RuntimeStatus(
        "12345678-1234-1234-1234-123456789abc", 1, "a" * 64, "b" * 64, "c" * 64,
        1, False, "game.exe", 0, (), ((123, "good\u202ebad.exe"),),
    )
    with pytest.raises(ValueError, match="unsafe display controls"):
        render_status(status)


def test_runtime_command_generations_cannot_skip_or_exhaust_session_space(tmp_path: Path):
    store, prepared, *_ = _prepared_session_fixture(tmp_path)
    with pytest.raises(ValueError, match="next generation is 1"):
        store.write_commands(prepared, [RuntimeCommand(2, "list_processes")])
    with pytest.raises(ValueError, match="consecutive"):
        store.write_commands(prepared, [RuntimeCommand(1, "list_processes"), RuntimeCommand(3, "list_processes")])
    assert store.next_generation(prepared) == 1


def test_status_read_tolerates_the_bridge_non_atomic_replace_window(tmp_path: Path, monkeypatch):
    """A live bridge must not read as disconnected during its own status replace.

    Wine's `rename` cannot overwrite an existing name, so the bridge unlinks
    `status.txt` before renaming its staged copy into place. Only that window --
    identified by the staged file -- earns one bounded re-read; a session whose
    bridge never wrote a heartbeat still resolves as absent immediately.
    """
    store = SessionStore(tmp_path / "state", tmp_path)
    status_path = tmp_path / "status.txt"
    staged_path = tmp_path / "status.txt.tmp"

    slept: list[float] = []
    monkeypatch.setattr(session_protocol.time, "sleep", lambda seconds: slept.append(seconds))

    assert store._read_status_bytes(status_path) == (None, None)
    assert slept == []

    payload = b"CEDECKY-STATUS-1\n"
    staged_path.write_bytes(payload)

    def complete_replace(seconds: float) -> None:
        slept.append(seconds)
        status_path.write_bytes(payload)

    monkeypatch.setattr(session_protocol.time, "sleep", complete_replace)
    data, info = store._read_status_bytes(status_path)
    assert data == payload
    assert info is not None
    assert slept == [session_protocol.STATUS_REPLACE_RETRY_SECONDS]

    # A staged file that never lands still resolves as absent rather than hanging.
    status_path.unlink()
    monkeypatch.setattr(session_protocol.time, "sleep", lambda seconds: slept.append(seconds))
    assert store._read_status_bytes(status_path) == (None, None)
    assert len(slept) == 2



def test_autoload_remembered_state_overlays_legacy_startup_only_when_enabled(tmp_path: Path):
    source = tmp_path / "game.CT"
    source.write_bytes(CT)
    tables = TableStore(tmp_path / "tables")
    artifact = tables.import_ct(str(source))
    blob = tables.verified_blob(artifact.sha256)
    inspection = inspect_table(blob, artifact.sha256)
    profiles = ProfileStore(tmp_path / "profiles.json")
    profiles.upsert(app_id=142, name="Game", is_shortcut=False, table_sha256=artifact.sha256, target_process="game.exe")
    profiles.set_startup(app_id=142, table_sha256=artifact.sha256, record_id=2, active=True, value="1")
    profiles.set_execution_consent(app_id=142, table_sha256=artifact.sha256, consent=True)
    profiles.set_remembered(app_id=142, table_sha256=artifact.sha256, states=[StartupPreference(2, False, None)])

    disabled = SessionStore(tmp_path / "state-disabled", tmp_path).prepare(profiles.get(142), blob, inspection, CE_SHA)  # type: ignore[arg-type]
    disabled_actions = parse_descriptor(Path(disabled.descriptor_path).read_bytes()).startup
    assert [(item.kind, item.value) for item in disabled_actions] == [("value", "1"), ("active", "1")]

    profiles.set_autoload(app_id=142, table_sha256=artifact.sha256, enabled=True)
    enabled = SessionStore(tmp_path / "state-enabled", tmp_path).prepare(profiles.get(142), blob, inspection, CE_SHA)  # type: ignore[arg-type]
    enabled_actions = parse_descriptor(Path(enabled.descriptor_path).read_bytes()).startup
    assert [(item.kind, item.value) for item in enabled_actions] == [("value", "1"), ("active", "0")]


def test_remembered_value_overlay_preserves_startup_active_field():
    from ce_decky.profiles import GameProfile, StartupPreference
    from ce_decky.session_protocol import _effective_startup_preferences

    profile = GameProfile(
        app_id=99, name="Game", is_shortcut=False, table_sha256="a" * 64, target_process="game.exe",
        startup=[StartupPreference(5, True, "10")],
        remembered=[StartupPreference(5, None, "20")],
        autoload_enabled=True,
    )
    assert _effective_startup_preferences(profile) == [StartupPreference(5, True, "20")]


def test_configured_value_overrides_the_live_value_autoload_would_otherwise_restore():
    """Auto-load writes the value the user configured, not the one CE last read.

    The remembered state is a reconciliation of a live session, so a record that
    was unreadable or that the game had already changed put the wrong number
    back on the next launch. The configured value is the user's own answer and
    wins for the same MemoryRecord, while the remembered active state stands.
    """
    from ce_decky.profiles import ConfiguredValue, GameProfile, StartupPreference
    from ce_decky.session_protocol import _effective_startup_preferences

    profile = GameProfile(
        app_id=99, name="Game", is_shortcut=False, table_sha256="a" * 64, target_process="game.exe",
        startup=[StartupPreference(5, True, "10")],
        remembered=[StartupPreference(5, None, "0")],
        configured_values=[ConfiguredValue(5, "9999"), ConfiguredValue(6, "1")],
        autoload_enabled=True,
    )
    assert _effective_startup_preferences(profile) == [
        StartupPreference(5, True, "9999"),
        StartupPreference(6, None, "1"),
    ]

    # Without auto-load only the explicit legacy startup state executes.
    stopped = GameProfile(
        app_id=99, name="Game", is_shortcut=False, table_sha256="a" * 64, target_process="game.exe",
        startup=[StartupPreference(5, True, "10")],
        configured_values=[ConfiguredValue(5, "9999")],
        autoload_enabled=False,
    )
    assert _effective_startup_preferences(stopped) == [StartupPreference(5, True, "10")]


def test_startup_actions_switch_on_the_scripts_that_create_a_remembered_record():
    from types import SimpleNamespace
    from ce_decky.profiles import StartupPreference
    from ce_decky.session_protocol import _startup_actions

    def control(record_id, path, kind):
        return SimpleNamespace(
            id=record_id, path=tuple(path), kind=kind, group_header=False,
            dropdown_read_only=False, dropdown_values=(),
        )

    inspection = SimpleNamespace(controls=[
        control(1, ["Party Damage Reduction"], "script"),
        control(2, ["Party Damage Reduction", "Reduction %"], "value"),
    ])
    actions = _startup_actions([StartupPreference(2, True, "95")], inspection)  # type: ignore[arg-type]
    # The enclosing script is switched on first; without it the inner record has
    # no address for the value write that follows.
    assert [(item.record_id, item.kind, item.value) for item in actions] == [
        (1, "active", "1"), (2, "value", "95"), (2, "active", "1"),
    ]


def _plan_control(record_id, path, kind, *, attach_only=False):
    from types import SimpleNamespace
    return SimpleNamespace(
        id=record_id, path=tuple(path), kind=kind, group_header=False,
        dropdown_read_only=False, dropdown_values=(), attach_only=attach_only,
    )


def test_a_startup_plan_refuses_a_record_whose_enclosing_script_is_ambiguous():
    """A dependency that cannot be addressed is not a dependency that can be dropped.

    Two MemoryRecords in the exact table share the enclosing script's ID, so no
    command names one of them. Skipping it silently left a plan that validated
    and then waited for a child Cheat Engine never creates, with the timeout
    reported against the child instead of the script that could not be run.
    """
    from types import SimpleNamespace
    from ce_decky.profiles import StartupPreference
    from ce_decky.session_protocol import _startup_actions

    inspection = SimpleNamespace(controls=[
        _plan_control(1, ["Master script"], "script"),
        _plan_control(2, ["Master script", "Inf. AP"], "value"),
        _plan_control(1, ["Elsewhere"], "script"),
    ])
    with pytest.raises(ValueError, match="needs enclosing script 1, which is ambiguous"):
        _startup_actions([StartupPreference(2, True, None)], inspection)  # type: ignore[arg-type]


def test_a_startup_plan_never_replays_the_tables_own_attach_record():
    """Attach-only records are machinery and are not carried by Auto-load.

    CE Decky attached to the exact process long before any startup plan runs,
    and switching the table author's own attach record on re-opens that process
    by name, which can move Cheat Engine off the PID it was given. A profile
    written before this was recognised can still remember one as active.
    """
    from types import SimpleNamespace
    from ce_decky.profiles import StartupPreference
    from ce_decky.session_protocol import _startup_actions

    inspection = SimpleNamespace(controls=[
        _plan_control(1, ["Attach to process"], "script", attach_only=True),
        _plan_control(2, ["Inf. Health"], "script"),
    ])
    actions = _startup_actions(
        [StartupPreference(1, True, None), StartupPreference(2, True, None)], inspection,  # type: ignore[arg-type]
    )
    assert [(item.record_id, item.kind) for item in actions] == [(2, "active")]


def _session_store(tmp_path: Path):
    """A store with one consented profile, ready to prepare sessions repeatedly."""
    source = tmp_path / "game.CT"
    source.write_bytes(CT)
    tables = TableStore(tmp_path / "tables")
    artifact = tables.import_ct(str(source))
    blob = tables.verified_blob(artifact.sha256)
    inspection = inspect_table(blob, artifact.sha256)
    profiles = ProfileStore(tmp_path / "profiles.json")
    profiles.upsert(app_id=42, name="Game", is_shortcut=False, table_sha256=artifact.sha256, target_process="game.exe")
    profiles.set_execution_consent(app_id=42, table_sha256=artifact.sha256, consent=True)
    profile = profiles.get(42)
    assert profile is not None
    return SessionStore(tmp_path / "state", tmp_path), profile, blob, inspection


def _session_dirs(sessions: SessionStore, app_id: int = 42) -> set[str]:
    return {entry.name for entry in (sessions.root / str(app_id)).iterdir() if entry.is_dir()}


def _stamp(sessions: SessionStore, session_id: str, when: float, app_id: int = 42) -> None:
    """Give one session an exact age, so collection order is not a race."""
    metadata = sessions.root / str(app_id) / session_id / "session.json"
    os.utime(metadata, (when, when))


def test_ordinary_play_cannot_grow_the_session_store_without_bound(tmp_path: Path):
    # Every prepare writes a full table snapshot, and the launch/stop/switch loop
    # prepares one each time, so an uncollected store grows with ordinary play.
    sessions, profile, blob, inspection = _session_store(tmp_path)
    prepared = [sessions.prepare(profile, blob, inspection, CE_SHA) for _ in range(6)]
    remaining = _session_dirs(sessions)
    assert len(remaining) == RETAINED_SESSION_HISTORY + 1
    # The session just prepared is the current one and is never collected.
    assert prepared[-1].session_id in remaining
    assert remaining <= {item.session_id for item in prepared}


def test_session_collection_keeps_the_newest_evidence_and_drops_the_rest(tmp_path: Path):
    sessions, profile, blob, inspection = _session_store(tmp_path)
    prepared = [item.session_id for item in
                (sessions.prepare(profile, blob, inspection, CE_SHA) for _ in range(4))]
    assert _session_dirs(sessions) == set(prepared)
    # Exact ages, oldest first, so the choice is the rule rather than the clock.
    for index, session_id in enumerate(prepared):
        _stamp(sessions, session_id, 1_000_000 + index)
    current = prepared[-1]
    removed = sessions.collect_retired_sessions(42, retain=1)
    # The current session, plus the newest one after it, and nothing else.
    assert _session_dirs(sessions) == {current, prepared[2]}
    assert set(removed) == {prepared[0], prepared[1]}


def test_a_live_session_is_kept_by_name_however_old_it_is(tmp_path: Path):
    sessions, profile, blob, inspection = _session_store(tmp_path)
    prepared = [sessions.prepare(profile, blob, inspection, CE_SHA) for _ in range(4)]
    surviving = [item.session_id for item in prepared]
    assert _session_dirs(sessions) == set(surviving)
    oldest = surviving[0]
    for index, session_id in enumerate(surviving):
        _stamp(sessions, session_id, 1_000_000 + index)
    # A caller owning a live Cheat Engine names its own session; age is irrelevant.
    removed = sessions.collect_retired_sessions(42, keep=oldest, retain=1)
    # The named session survives as the oldest of all; the newest other is the
    # one kept as evidence, and the two between them go.
    assert _session_dirs(sessions) == {oldest, surviving[3]}
    assert set(removed) == {surviving[1], surviving[2]}


def test_collection_never_deletes_a_directory_its_own_metadata_does_not_identify(tmp_path: Path):
    sessions, profile, blob, inspection = _session_store(tmp_path)
    sessions.prepare(profile, blob, inspection, CE_SHA)
    app_root = sessions.root / "42"

    bare = app_root / "11111111-1111-4111-8111-111111111111"
    bare.mkdir()

    mismatched = app_root / "22222222-2222-4222-8222-222222222222"
    mismatched.mkdir()
    (mismatched / "session.json").write_text(
        json.dumps({"schema": 3, "session_id": "33333333-3333-4333-8333-333333333333", "app_id": 42}),
        encoding="utf-8",
    )

    foreign = app_root / "44444444-4444-4444-8444-444444444444"
    foreign.mkdir()
    (foreign / "session.json").write_text(
        json.dumps({"schema": 3, "session_id": foreign.name, "app_id": 99}), encoding="utf-8"
    )

    removed = sessions.collect_retired_sessions(42, retain=0)
    remaining = _session_dirs(sessions)
    # None of the three identifies itself, so the corrupt-state path reports them
    # rather than this collector deleting them silently.
    assert {bare.name, mismatched.name, foreign.name} <= remaining
    assert not set(removed) & {bare.name, mismatched.name, foreign.name}


def test_collection_stands_down_when_the_current_pointer_cannot_be_read(tmp_path: Path):
    sessions, profile, blob, inspection = _session_store(tmp_path)
    for _ in range(4):
        sessions.prepare(profile, blob, inspection, CE_SHA)
    before = _session_dirs(sessions)
    (sessions.root / "42" / "current.json").write_text("{ not json", encoding="utf-8")
    # Collecting against a pointer we could not read could delete the live session.
    assert sessions.collect_retired_sessions(42, retain=0) == []
    assert _session_dirs(sessions) == before


def test_pointer_repair_reports_post_unlink_durability_failure(tmp_path, monkeypatch):
    from ce_decky import atomic
    store = SessionStore(tmp_path / "state", tmp_path)
    pointer = store.root / "10" / "current.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_text("corrupt")
    def refuse(path):
        raise OSError("sync refused")
    monkeypatch.setattr(atomic, "fsync_directory", refuse)
    with pytest.raises(atomic.DurabilityUnknownError):
        store.discard_current_pointer(10)
    assert not pointer.exists()


def test_focus_telemetry_round_trips_and_rejects_unknown_results():
    status = RuntimeStatus(SESSION, 42, CE_SHA, "b" * 64, "d" * 64, 1234, True, "game.exe", 555, (), (),
                           focus_candidates=1, focus_attempts=6, focus_successes=0, focus_reason="refused")
    encoded = render_status(status)
    assert parse_status(encoded) == status
    with pytest.raises(ValueError, match="focus reason"):
        parse_status(encoded.replace(b"refused", b"invented"))
    with pytest.raises(ValueError):
        parse_status(encoded.replace(b"focus_attempts\t6", b"focus_attempts\t-1"))


def test_startup_summary_is_bounded_unique_and_independent_of_runtime_results():
    from dataclasses import replace
    status = RuntimeStatus("123e4567-e89b-42d3-a456-426614174000", 10, "a" * 64, "b" * 64,
        "c" * 64, 1, True, "game.exe", 123, (), (), startup_active_ids=(2048,), focus_discovery_attempts=20)
    encoded = render_status(status)
    assert parse_status(encoded).startup_active_ids == (2048,)
    assert parse_status(encoded).focus_discovery_attempts == 20
    with pytest.raises(ValueError):
        parse_status(encoded + b"S\t2048\n")
    with pytest.raises(ValueError):
        render_status(replace(status, startup_active_ids=tuple(range(2049))))
    with pytest.raises(ValueError):
        parse_status(encoded.replace(b"focus_discovery_attempts\t20", b"focus_discovery_attempts\t21"))


SWITCH_CT = b'''<CheatTable CheatEngineTableVersion="45"><CheatEntries>
<CheatEntry><ID>2</ID><Description>"Godmode"</Description><VariableType>4 Bytes</VariableType><DropDownList>0:Disabled\n1:Enabled</DropDownList></CheatEntry>
<CheatEntry><ID>3</ID><Description>"Ammo"</Description><VariableType>4 Bytes</VariableType><DropDownList>10:Ten\n20:Twenty\n30:Thirty</DropDownList></CheatEntry>
<CheatEntry><ID>4</ID><Description>"Script"</Description><VariableType>Auto Assembler Script</VariableType><AssemblerScript>[ENABLE]
</AssemblerScript></CheatEntry>
</CheatEntries></CheatTable>'''


def test_the_descriptor_carries_the_key_each_switch_is_off_at(tmp_path: Path):
    """Releasing a frozen record leaves the value it was frozen at in the game.

    The stop asks the bridge to put this session's records down, and the bridge
    reads records rather than the table's own labels, so which key each switch
    is off at travels with the session. Only the records the panel draws as
    switches: a list of three things is a choice rather than a switch, and an
    Auto Assembler record is switched off by running its own `[DISABLE]`.
    """
    source = tmp_path / "switches.CT"
    source.write_bytes(SWITCH_CT)
    tables = TableStore(tmp_path / "tables")
    artifact = tables.import_ct(str(source))
    blob = tables.verified_blob(artifact.sha256)
    inspection = inspect_table(blob, artifact.sha256)

    profiles = ProfileStore(tmp_path / "profiles.json")
    profiles.upsert(app_id=42, name="Game", is_shortcut=False, table_sha256=artifact.sha256, target_process="game.exe")
    profiles.set_execution_consent(app_id=42, table_sha256=artifact.sha256, consent=True)
    profile = profiles.get(42)
    assert profile is not None
    prepared = SessionStore(tmp_path / "state", tmp_path).prepare(profile, blob, inspection, CE_SHA)

    descriptor = parse_descriptor(Path(prepared.descriptor_path).read_bytes())
    assert descriptor.switch_off == ((2, "0"),)


def test_a_descriptor_states_one_off_value_per_record(tmp_path: Path):
    """Two of them for one MemoryRecord is a descriptor nothing may act on."""
    sid = str(uuid.uuid4())
    rendered = render_descriptor(SessionDescriptor(
        sid, 42, CE_SHA, "b" * 64, "Z:\\s\\table.ct", "game.exe",
        "Z:\\s\\control.txt", "Z:\\s\\status.txt", (), False, False, ((2, "0"), (3, "10")),
    ))
    assert parse_descriptor(rendered).switch_off == ((2, "0"), (3, "10"))
    with pytest.raises(ValueError, match="duplicate switch off value"):
        parse_descriptor(rendered + b"S\t2\t1\n")
    with pytest.raises(ValueError, match="duplicate switch off value"):
        render_descriptor(SessionDescriptor(
            sid, 42, CE_SHA, "b" * 64, "Z:\\s\\table.ct", "game.exe",
            "Z:\\s\\control.txt", "Z:\\s\\status.txt", (), False, False, ((2, "0"), (2, "1")),
        ))
    # And a session prepared before this existed still parses, with none stated.
    legacy = b"".join(line + b"\n" for line in rendered.split(b"\n") if line and not line.startswith(b"S\t"))
    assert parse_descriptor(legacy).switch_off == ()
