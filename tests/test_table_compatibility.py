import json

import pytest

from ce_decky.table_compatibility import (
    BACKUP_NAME,
    COMPARISON_EXECUTABLE,
    COMPARISON_VERSION_BUILD,
    TableCompatibility,
    comparison_basis,
    compatibility_state,
    steam_build_id,
)


@pytest.mark.parametrize('old,current,expected', [
    ({'pe_version': '1'}, {'pe_version': '1'}, 'matching'),
    ({'pe_version': '1'}, {'pe_version': '2'}, 'retest'),
    ({}, {}, 'unknown'),
    ({'steam_build_id': '1'}, {'pe_version': '2'}, 'unknown'),
    ({'pe_version': '1'}, {'pe_version': '1', 'steam_build_id': '2'}, 'matching'),
    ({'pe_version': '1', 'steam_build_id': '1'}, {'pe_version': '1', 'steam_build_id': '2'}, 'retest'),
])
def test_compatible_signals_must_all_match(old, current, expected):
    entry = {'pe_version': None, 'steam_build_id': None, 'invalidated': False,
             'comparison': COMPARISON_VERSION_BUILD, 'executable_sha256': None, **old}
    assert compatibility_state(entry, current) == expected


A, B = 'a' * 64, 'b' * 64


@pytest.mark.parametrize('saved,current,expected', [
    # The same bytes are the same program.
    ({'executable_sha256': A}, {'executable_sha256': A}, 'matching'),
    # Different bytes are a different program, whatever its version says now.
    ({'executable_sha256': A}, {'executable_sha256': B, 'pe_version': '1'}, 'retest'),
    ({'executable_sha256': A, 'pe_version': None}, {'executable_sha256': B}, 'retest'),
    # Nothing read now, or nothing read then, is nothing compared.
    ({'executable_sha256': A}, {}, 'unknown'),
    ({'executable_sha256': A}, {'executable_sha256': None, 'executable_state': 'pending'}, 'unknown'),
    ({'executable_sha256': None}, {'executable_sha256': A}, 'unknown'),
    # A version that turned up later is not what this success was observed
    # against, so it cannot stand in for the missing digest.
    ({'executable_sha256': None}, {'pe_version': '1', 'steam_build_id': '2'}, 'unknown'),
    ({'executable_sha256': A, 'invalidated': True}, {'executable_sha256': A}, 'retest'),
])
def test_an_executable_compared_record_is_judged_by_its_bytes_alone(saved, current, expected):
    entry = {'pe_version': None, 'steam_build_id': None, 'invalidated': False,
             'comparison': COMPARISON_EXECUTABLE, 'executable_sha256': None, **saved}
    assert compatibility_state(entry, current) == expected


def test_the_digest_is_the_basis_only_where_nothing_else_was_observed():
    assert comparison_basis({'pe_version': '1'}) == COMPARISON_VERSION_BUILD
    assert comparison_basis({'steam_build_id': '7'}) == COMPARISON_VERSION_BUILD
    assert comparison_basis({'pe_version': None, 'steam_build_id': None}) == COMPARISON_EXECUTABLE
    assert comparison_basis({}) == COMPARISON_EXECUTABLE


def test_a_digest_is_kept_only_on_a_record_compared_by_one(tmp_path):
    store = TableCompatibility(tmp_path / 'compatibility.json')
    kept = store.record(10, A, 'game.exe', {'executable_sha256': B, 'executable_path': '/g/game.exe'}, failure_epoch='0' * 32)
    assert (kept['comparison'], kept['executable_sha256']) == (COMPARISON_EXECUTABLE, B)
    # A version was observed, so the version is the basis and a digest that
    # came along with it is a claim nothing would read.
    dropped = store.record(11, A, 'game.exe', {'pe_version': '1', 'executable_sha256': B}, failure_epoch='0' * 32)
    assert (dropped['comparison'], dropped['executable_sha256']) == (COMPARISON_VERSION_BUILD, None)
    assert [row['comparison'] for row in store.snapshot()['entries']] == [COMPARISON_EXECUTABLE, COMPARISON_VERSION_BUILD]


def _row(**fields):
    return {'app_id': 10, 'table_sha256': A, 'target_process': 'game.exe', 'pe_version': None,
            'steam_build_id': None, 'last_working_at': 1700000000, 'invalidated': False,
            'failure_epoch': '0' * 32, 'executable_path': None, 'executable_sha256': None,
            'comparison': COMPARISON_EXECUTABLE, **fields}


@pytest.mark.parametrize('fields', [
    {'comparison': 'version'},
    {'executable_sha256': 'A' * 64},
    {'executable_sha256': 'a' * 63},
    {'executable_sha256': A, 'comparison': COMPARISON_VERSION_BUILD},
])
def test_an_executable_digest_is_validated_strictly(tmp_path, fields):
    path = tmp_path / 'compatibility.json'
    path.write_text(json.dumps({'schema': 4, 'entries': [_row(**fields)]}), encoding='utf-8')
    snapshot = TableCompatibility(path).snapshot()
    assert snapshot['entries'] == [] and snapshot['reason']


def test_an_older_record_that_claims_a_newer_field_is_refused(tmp_path):
    path = tmp_path / 'compatibility.json'
    row = _row(executable_sha256=A)
    path.write_text(json.dumps({'schema': 3, 'entries': [row]}), encoding='utf-8')
    snapshot = TableCompatibility(path).snapshot()
    assert snapshot['entries'] == [] and snapshot['reason']


def test_a_late_digest_reaches_only_the_success_it_was_read_for(tmp_path):
    store = TableCompatibility(tmp_path / 'compatibility.json')
    row = store.record(10, A, 'game.exe', {'executable_path': '/g/game.exe'}, failure_epoch='0' * 32)
    other = store.record(20, A, 'game.exe', {'executable_path': '/h/game.exe'}, failure_epoch='0' * 32)
    assert store.add_executable_digest(row, B) is True
    entries = {entry['app_id']: entry for entry in store.snapshot()['entries']}
    assert entries[10]['executable_sha256'] == B and entries[20]['executable_sha256'] is None
    # Once given, never replaced.
    assert store.add_executable_digest({**row, 'executable_sha256': None}, A) is False
    # A row a failure retired, or a newer success, is not that success.
    store.invalidate(A)
    assert store.add_executable_digest(other, B) is False
    newer = store.record(20, A, 'game.exe', {'executable_path': '/h/game.exe'}, failure_epoch='0' * 32)
    assert store.add_executable_digest({**other, 'last_working_at': newer['last_working_at'] - 1}, B) is False
    # A record compared by version never takes a digest.
    versioned = store.record(30, A, 'game.exe', {'pe_version': '1'}, failure_epoch='0' * 32)
    assert store.add_executable_digest(versioned, B) is False
    with pytest.raises(ValueError):
        store.add_executable_digest(newer, 'not a digest')


def test_failure_invalidation_survives_reopen_until_new_success(tmp_path):
    path = tmp_path / 'compatibility.json'
    store = TableCompatibility(path)
    store.record(10, 'a' * 64, 'game.exe', {'pe_version': '1'}, failure_epoch="0" * 32)
    store.invalidate('a' * 64)
    entry = TableCompatibility(path).snapshot()['entries'][0]
    assert compatibility_state(entry, {'pe_version': '1'}) == 'retest'
    store.record(10, 'a' * 64, 'game.exe', {'pe_version': '2'}, failure_epoch="0" * 32)
    assert compatibility_state(store.snapshot()['entries'][0], {'pe_version': '2'}) == 'matching'


def test_the_shape_before_this_one_keeps_every_proof_it_held(tmp_path):
    # The exact file 0.9.24 wrote: every field this reader wants except the
    # executable path, which is a field a record is allowed not to have. Refused,
    # it was not merely invisible - `record()` reads an unreadable file as no
    # history, so the first table proven after the upgrade would have rewritten
    # the file with itself as its only row.
    path = tmp_path / 'compatibility.json'
    def row(app_id, digest, version):
        return {'app_id': app_id, 'table_sha256': digest, 'target_process': 'game.exe',
                'pe_version': version, 'steam_build_id': None, 'last_working_at': 1700000000,
                'invalidated': False, 'failure_epoch': '0' * 32}
    path.write_text(json.dumps({'schema': 2, 'entries': [
        row(220, 'a' * 64, '1'), row(70, 'b' * 64, '2')]}), encoding='utf-8')
    store = TableCompatibility(path)

    snapshot = store.snapshot()
    assert snapshot['reason'] is None and snapshot['schema'] == 4
    assert [(entry['app_id'], entry['table_sha256']) for entry in snapshot['entries']] == [(220, 'a' * 64), (70, 'b' * 64)]
    # Stated absent rather than guessed at, so the row proves exactly what it
    # proved before: still a match against the build it was recorded on.
    assert [entry['executable_path'] for entry in snapshot['entries']] == [None, None]
    assert [(entry['comparison'], entry['executable_sha256']) for entry in snapshot['entries']] == [
        (COMPARISON_VERSION_BUILD, None)] * 2
    assert compatibility_state(snapshot['entries'][0], {'pe_version': '1'}) == 'matching'

    # And proving one more table keeps the rest of the device's history.
    store.record(10, 'c' * 64, 'other.exe', {'pe_version': '3'}, failure_epoch='0' * 32)
    written = json.loads(path.read_text(encoding='utf-8'))
    assert written['schema'] == 4
    assert [entry['table_sha256'] for entry in written['entries']] == ['a' * 64, 'b' * 64, 'c' * 64]


def _schema3_row(app_id, digest, **fields):
    return {'app_id': app_id, 'table_sha256': digest, 'target_process': 'game.exe', 'pe_version': None,
            'steam_build_id': None, 'last_working_at': 1700000000 + app_id, 'invalidated': False,
            'failure_epoch': 'f' * 32, 'executable_path': None, **fields}


@pytest.mark.parametrize('schema', [2, 3])
def test_migration_keeps_every_row_and_the_original(tmp_path, schema):
    path = tmp_path / 'compatibility.json'
    rows = [
        _schema3_row(10, A, pe_version='1.0', executable_path='/g/a/game.exe'),
        _schema3_row(20, B, steam_build_id='123', invalidated=True),
        _schema3_row(30, 'c' * 64, executable_path='/g/c/game.exe'),
    ]
    if schema == 2:
        rows = [{key: value for key, value in row.items() if key != 'executable_path'} for row in rows]
    original = json.dumps({'schema': schema, 'entries': rows}).encode()
    path.write_bytes(original)
    backup = tmp_path / BACKUP_NAME
    store = TableCompatibility(path)

    outcome = store.migrate(backup)

    assert outcome == {'outcome': 'migrated', 'schema': schema, 'rows': 3, 'backup_kept': False}
    assert backup.read_bytes() == original
    written = json.loads(path.read_text(encoding='utf-8'))
    assert written['schema'] == 4
    # Exactly the rows the original held, with only the fields this version
    # added and the values that claim nothing: identities, times, epochs,
    # invalidation and observations all unchanged, and no digest filled in.
    added = {'executable_sha256': None, 'comparison': COMPARISON_VERSION_BUILD}
    if schema == 2:
        added['executable_path'] = None
    assert written['entries'] == [{**row, **added} for row in rows]
    # What this version would have written is what is there: a second load
    # finds it current, and the next record keeps every row.
    assert store.migrate(backup) == {'outcome': 'current'}
    store.record(40, 'd' * 64, 'game.exe', {'pe_version': '2'}, failure_epoch='f' * 32)
    assert [row['app_id'] for row in store.snapshot()['entries']] == [10, 20, 30, 40]
    assert backup.read_bytes() == original


def test_migration_never_overwrites_an_earlier_backup(tmp_path):
    path = tmp_path / 'compatibility.json'
    backup = tmp_path / BACKUP_NAME
    backup.write_bytes(b'{"the": "first original"}')
    path.write_text(json.dumps({'schema': 3, 'entries': [_schema3_row(10, A)]}), encoding='utf-8')
    assert TableCompatibility(path).migrate(backup)['backup_kept'] is True
    assert backup.read_bytes() == b'{"the": "first original"}'


@pytest.mark.parametrize('body', [
    '{"schema":3,"entries":[{}]}',
    '{"schema":1,"entries":[]}',
    'not json',
])
def test_migration_leaves_a_record_it_cannot_read_exactly_as_it_was(tmp_path, body):
    path = tmp_path / 'compatibility.json'
    path.write_text(body, encoding='utf-8')
    backup = tmp_path / BACKUP_NAME
    outcome = TableCompatibility(path).migrate(backup)
    assert outcome['outcome'] == 'unreadable' and outcome['reason']
    assert path.read_text(encoding='utf-8') == body
    # Copied aside all the same: the next success reads it as no history and
    # replaces it, and the copy is then all that is left of it.
    assert backup.read_text(encoding='utf-8') == body


def test_migration_of_nothing_writes_nothing(tmp_path):
    path = tmp_path / 'compatibility.json'
    assert TableCompatibility(path).migrate(tmp_path / BACKUP_NAME) == {'outcome': 'missing'}
    assert not path.exists() and not (tmp_path / BACKUP_NAME).exists()


def test_corrupt_evidence_never_creates_a_claim(tmp_path):
    path = tmp_path / 'compatibility.json'
    path.write_text('{"schema":1,"entries":[{}]}')
    snapshot = TableCompatibility(path).snapshot()
    assert snapshot['entries'] == [] and snapshot['reason']


@pytest.mark.parametrize('body,expected', [
    ('"AppState" { "appid" "10" "buildid" "123" }', '123'),
    ('"AppState" { "appid" "11" "buildid" "123" }', None),
    ('"AppState" { "appid" "10" "buildid" "123" "buildid" "456" }', None),
    ('"AppState" { "appid" "10" "buildid" "123" ', None),
    ('"AppState" { "appid" "10" "buildid" "unknown" }', None),
])
def test_manifest_build_is_bound_to_appstate_identity(tmp_path, body, expected):
    root = tmp_path / '.local/share/Steam/steamapps'
    root.mkdir(parents=True)
    manifest = root / 'appmanifest_10.acf'
    manifest.write_text(body)
    assert steam_build_id(tmp_path, 10) == expected
    assert manifest.read_text() == body


@pytest.mark.parametrize("damage", ["old_schema", "missing_epoch", "old_epoch"])
def test_only_complete_current_evidence_survives_and_new_success_recreates_it(tmp_path, damage):
    import json
    path = tmp_path / "compatibility.json"
    store = TableCompatibility(path)
    store.record(10, "a" * 64, "game.exe", {"pe_version": "1"}, failure_epoch="0" * 32)
    payload = json.loads(path.read_text())
    if damage == "old_schema":
        payload["schema"] = 1
    elif damage == "missing_epoch":
        del payload["entries"][0]["failure_epoch"]
    else:
        payload["entries"][0]["failure_epoch"] = "legacy"
    path.write_text(json.dumps(payload))
    assert store.snapshot()["entries"] == []
    store.record(10, "b" * 64, "game.exe", {"pe_version": "2"}, failure_epoch="0" * 32)
    assert [row["table_sha256"] for row in store.snapshot()["entries"]] == ["b" * 64]


def test_initial_failure_epoch_is_stable_before_the_first_failure(tmp_path):
    from ce_decky.table_blocklist import TableBlocklist
    blocked = TableBlocklist(tmp_path / "blocked.json")
    first = blocked.failure_epochs()
    assert first == TableBlocklist(blocked.path).failure_epochs()
    blocked.clear()
    assert blocked.failure_epochs() == first


@pytest.mark.parametrize("process", [None, "", "game", "sub/game.exe", "game.exe" + chr(0)])
def test_evidence_without_a_usable_target_process_is_unreadable_rather_than_a_claim(tmp_path, process):
    import json
    path = tmp_path / "compatibility.json"
    store = TableCompatibility(path)
    store.record(10, "a" * 64, "game.exe", {"pe_version": "1"}, failure_epoch="0" * 32)
    payload = json.loads(path.read_text())
    payload["entries"][0]["target_process"] = process
    path.write_text(json.dumps(payload))
    snapshot = TableCompatibility(path).snapshot()
    assert snapshot["entries"] == [] and snapshot["reason"]


def test_recording_evidence_for_an_unusable_target_process_writes_nothing(tmp_path):
    path = tmp_path / "compatibility.json"
    store = TableCompatibility(path)
    with pytest.raises(ValueError):
        store.record(10, "a" * 64, None, {"pe_version": "1"}, failure_epoch="0" * 32)
    assert not path.exists()


def test_the_path_a_fingerprint_was_read_from_is_kept_with_the_evidence(tmp_path):
    """Without it a proven table is only comparable while the game is running.

    `pe_version` is read off the game's own live executable and a non-Steam
    shortcut has no build id at all, so a shortcut's green went to "current
    build unknown" the moment the game closed, which is the state Manage is
    usually opened in. The path is knowable at exactly one moment, while the
    evidence is being recorded, and it answers the same question off the disk
    afterwards.
    """
    store = TableCompatibility(tmp_path / "compatibility.json")
    store.record(10, "a" * 64, "game.exe", {
        "pe_version": "1.2", "steam_build_id": None, "executable_path": "/games/Game/game.exe",
    }, failure_epoch="0" * 32)
    held = TableCompatibility(store.path).snapshot()["entries"]
    assert held[0]["executable_path"] == "/games/Game/game.exe"

    # A record made without one is written and read exactly as before.
    store.record(11, "b" * 64, "game.exe", {"pe_version": "1.2", "steam_build_id": None},
                 failure_epoch="0" * 32)
    kept = {row["app_id"]: row for row in TableCompatibility(store.path).snapshot()["entries"]}
    assert kept[11]["executable_path"] is None


@pytest.mark.parametrize("path", ["relative/game.exe", " /games/game.exe", "/games/\x00game.exe", 42])
def test_an_executable_path_that_is_not_one_makes_the_record_unreadable(tmp_path, path):
    """It is stored state that a later read opens a file from, so it is validated
    exactly as strictly as every other field here rather than trusted."""
    import json
    target = tmp_path / "compatibility.json"
    target.write_text(json.dumps({"schema": 3, "entries": [{
        "app_id": 10, "table_sha256": "a" * 64, "target_process": "game.exe",
        "pe_version": "1.2", "steam_build_id": None, "executable_path": path,
        "last_working_at": 1, "invalidated": False, "failure_epoch": "0" * 32,
    }]}), encoding="utf-8")
    held = TableCompatibility(target).snapshot()
    assert held["entries"] == []
    assert held["reason"]
