"""Bounded positive evidence, independent of execution consent and failures."""
from pathlib import Path
import re
import threading
import time

from .atomic import atomic_write_json, load_json, read_regular_bytes
from .managed_ce import discover_steam_library_roots
from .profiles import _optional_process

MAX_EVIDENCE = 4096
# 3 records, beside the fingerprint, the absolute path the fingerprint was read
# from.
#
# Without it a positive record can only be compared while the game is running.
# `pe_version` is read from the live process's own executable, and
# `steam_build_id` is `None` for a non-Steam shortcut by construction, so a
# shortcut's proven table had nothing comparable the moment the game closed and
# went from green to "current build unknown" - which is the state a user opens
# Manage in. The path is known at the one moment it can be: while the evidence
# is being recorded, with the game up. Read back later it answers the same
# question off the disk, for a Steam game as well as a shortcut.
#
# `None` where the path was not known, which is every record written before this
# and any recorded from a route that never saw one. Those compare exactly as
# they did, which is why the shape before this one is read rather than refused:
# 2 is this shape with the field absent, and absent is a value it has. See
# `_current_shape`.
SCHEMA = 3
# What an executable path may be before this refuses to store it. Absolute, and
# no longer than a path this filesystem can hold.
MAX_EXECUTABLE_PATH = 4096


def steam_build_id(user_home: Path, app_id: int, *, libraries=None) -> str | None:
    """Read one unambiguous AppState identity from the user's Steam libraries.

    Quoted KeyValues strings and objects only. Unsupported syntax, duplicate
    keys, mismatched AppID and multiple manifests supply no fingerprint.
    """
    def parse(text):
        matches = list(re.finditer(r'"(?:\\.|[^"\\])*"|[{}]|[^\s{}"]+', text))
        end = 0
        for match in matches:
            if text[end:match.start()].strip():
                raise ValueError("manifest token")
            end = match.end()
        if text[end:].strip():
            raise ValueError("manifest token")
        tokens = [match.group() for match in matches]
        index = 0
        def obj(depth=0):
            nonlocal index
            if depth > 16:
                raise ValueError("manifest nesting")
            result = {}
            while index < len(tokens) and tokens[index] != '}':
                key = tokens[index]
                index += 1
                if not key.startswith('"') or index >= len(tokens) or key in result:
                    raise ValueError("manifest key")
                value = tokens[index]
                index += 1
                if value == '{':
                    value = obj(depth + 1)
                elif not value.startswith('"'):
                    raise ValueError("manifest value")
                else:
                    value = value[1:-1]
                result[key] = value
            if depth:
                if index >= len(tokens):
                    raise ValueError("manifest object")
                index += 1
            return result
        value = obj()
        if index != len(tokens):
            raise ValueError("manifest trailing data")
        return value
    try:
        if libraries is None:
            _, libraries = discover_steam_library_roots(user_home)
        manifests = []
        for library in libraries:
            data = read_regular_bytes(library / f"steamapps/appmanifest_{app_id}.acf", max_bytes=256 * 1024, allow_missing=True)
            if data is None:
                continue
            state = parse(data.decode('utf-8')).get('"AppState"')
            if not isinstance(state, dict) or state.get('"appid"') != str(app_id):
                return None
            build = state.get('"buildid"')
            if not isinstance(build, str) or not re.fullmatch(r'[0-9]{1,20}', build):
                return None
            manifests.append(build)
        return manifests[0] if len(manifests) == 1 else None
    except (OSError, ValueError, RuntimeError):
        return None


def compatibility_state(entry: dict, fingerprint: dict) -> str:
    if entry['invalidated']:
        return 'retest'
    compared = [entry[key] == fingerprint.get(key) for key in ('pe_version', 'steam_build_id')
                if entry[key] is not None and fingerprint.get(key) is not None]
    if not all(compared):
        return 'retest'
    return 'matching' if compared else 'unknown'


def _current_shape(raw):
    """The record as this version writes it, reading the one shape before it.

    Schema 2 is schema 3 without `executable_path`, and that field is already
    one a record may not have: it is `None` for every route that never saw a
    path. So the row that comes out of here carries exactly the claims the row
    on disk carried and not one more - the absent field is stated absent, never
    guessed at - and it is then validated by the same strict reader as any other
    row, which is what keeps this from being a second, laxer way in.

    What refusing it cost was not the history alone. `record()` treats an
    unreadable file as no history, so the first table proven after the upgrade
    rewrote the file with itself as its only row, and every other proof on the
    device was gone for good.

    Anything else, this version's own shape included, is handed back exactly
    as it was read for that same reader to judge.
    """
    schema = raw.get('schema') if isinstance(raw, dict) else None
    if type(schema) is not int or schema != 2 or set(raw) != {'schema', 'entries'} or not isinstance(raw['entries'], list):
        return raw
    return {'schema': SCHEMA, 'entries': [
        {**row, 'executable_path': None} if isinstance(row, dict) and 'executable_path' not in row else row
        for row in raw['entries']]}


class TableCompatibility:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()

    def _load(self):
        return self._validated(_current_shape(load_json(self.path, {'schema': SCHEMA, 'entries': []})))

    @staticmethod
    def _validated(raw):
        if not isinstance(raw, dict) or set(raw) != {'schema', 'entries'} or type(raw['schema']) is not int or raw['schema'] != SCHEMA:
            raise ValueError('invalid compatibility schema')
        rows = raw['entries']
        if not isinstance(rows, list) or len(rows) > MAX_EVIDENCE:
            raise ValueError('invalid compatibility count')
        keys = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {'failure_epoch', 'app_id', 'table_sha256', 'target_process', 'pe_version', 'steam_build_id', 'last_working_at', 'invalidated', 'executable_path'}:
                raise ValueError('invalid compatibility entry')
            epoch = row['failure_epoch']
            if not isinstance(epoch, str) or not re.fullmatch('[0-9a-f]{32}', epoch):
                raise ValueError('invalid compatibility failure epoch')
            if type(row['app_id']) is not int or not 0 < row['app_id'] <= 0xFFFFFFFF or type(row['invalidated']) is not bool:
                raise ValueError('invalid compatibility identity')
            if not isinstance(row['table_sha256'], str) or not re.fullmatch('[0-9a-f]{64}', row['table_sha256']):
                raise ValueError('invalid compatibility digest')
            if type(row['last_working_at']) is not int or row['last_working_at'] < 0:
                raise ValueError('invalid compatibility time')
            # The process this evidence is about is what every comparison
            # against the current profile turns on, so it is data rather than a
            # fingerprint that may be absent: a row without one cannot be
            # compared, and a reader that assumed it was there crashed on the
            # whole status payload. Validated exactly as a profile validates it.
            path = row['executable_path']
            if path is not None and (
                not isinstance(path, str)
                or not path.startswith('/')
                or len(path) > MAX_EXECUTABLE_PATH
                or '\x00' in path
                or path != path.strip()
            ):
                raise ValueError('invalid compatibility executable path')
            try:
                process = _optional_process(row['target_process'])
            except ValueError as exc:
                raise ValueError('invalid compatibility target process') from exc
            if process is None:
                raise ValueError('invalid compatibility target process')
            for field in ('pe_version', 'steam_build_id'):
                value = row[field]
                if value is not None and (not isinstance(value, str) or not value or len(value.encode()) > 256 or any(ord(c) < 32 for c in value)):
                    raise ValueError('invalid compatibility fingerprint')
            key = (row['app_id'], row['table_sha256'])
            if key in keys:
                raise ValueError('duplicate compatibility identity')
            keys.add(key)
        return rows

    def snapshot(self):
        with self._lock:
            try:
                return {'schema': SCHEMA, 'entries': self._load(), 'reason': None}
            except (OSError, ValueError) as exc:
                return {'schema': SCHEMA, 'entries': [], 'reason': str(exc)[:256]}

    def record(self, app_id, digest, target_process, fingerprint, *, failure_epoch):
        with self._lock:
            try:
                previous = self._load()
            except ValueError:
                previous = []
            rows = [row for row in previous if (row['app_id'], row['table_sha256']) != (app_id, digest)]
            rows.append(dict(app_id=app_id, table_sha256=digest, target_process=target_process,
                             pe_version=fingerprint.get('pe_version'), steam_build_id=fingerprint.get('steam_build_id'),
                             executable_path=fingerprint.get('executable_path'),
                             last_working_at=int(time.time()), invalidated=False, failure_epoch=failure_epoch))
            payload = {'schema': SCHEMA, 'entries': rows[-MAX_EVIDENCE:]}
            self._validated(payload)
            atomic_write_json(self.path, payload)

    def invalidate(self, digest):
        with self._lock:
            rows = self._load()
            changed = False
            for row in rows:
                if row['table_sha256'] == digest and not row['invalidated']:
                    row['invalidated'] = True
                    changed = True
            if changed:
                atomic_write_json(self.path, {'schema': SCHEMA, 'entries': rows})
