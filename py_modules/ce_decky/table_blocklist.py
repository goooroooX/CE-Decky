"""Durable record of exact tables that were proven not to work.

A cheat table finds the game's code by scanning for byte patterns, so a table
written against an older build of the game does not misbehave - it simply
refuses to enable, every time, for as long as that build is installed. Nothing
about that is visible in a search result, so without a record the same bytes are
found, paid for with another provider countdown, downloaded, reviewed and
enabled again.

The record is keyed by the exact table SHA-256, which is the only identity that
survives a provider replacing a file, renaming it, or serving it from a
different post. That identity is also the whole problem for a search result: a
provider only rarely advertises a digest, so most rows cannot be recognised by
it at all, and the mark was invisible on exactly the source it was recorded
from. Each entry therefore also carries the provider rows the bytes were
downloaded from, which is the identity a result row does always have. It is deliberately durable and deliberately not automatic to
undo: a search that offers the same bytes again offers something already known
not to work. It is equally deliberately reversible by hand - a game update can
make a blocked table correct again - which is what the Advanced list is for.

This is advice and a refusal to re-import, never a trust decision: nothing here
authorizes a table, and a table already selected keeps working until the user
changes it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import re
import time
import unicodedata
from typing import Any

from uuid import uuid4

from .atomic import atomic_write_json, load_json
from .text import utf8_len

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_BLOCKED_TABLES = 512
MAX_REASON_BYTES = 1024
MAX_LABEL_BYTES = 1024
# Provider rows remembered per blocked table. One table's bytes reach a user
# from a handful of posts at most; a longer list is provider data growing
# without bound rather than useful recognition.
MAX_BLOCKED_ORIGINS = 8
# How far a submitted origin list is read before the newest of it is kept. A
# table carries more rows than a record keeps, so the whole submission is
# validated and de-duplicated first and the tail is what survives: the row a
# user most recently got these bytes from is the row that has to carry the mark.
_ORIGIN_SCAN_LIMIT = MAX_BLOCKED_ORIGINS * 8
MAX_ORIGIN_BYTES = 512

# Why an entry is in this list, as one token the panel can turn into a short
# status. The reason beside it is a sentence assembled from what Cheat Engine or
# the archive layer said, which is the right thing to read once and the wrong
# thing to classify by: the three causes are known exactly where the record is
# written, and nothing downstream should be inferring them back out of prose.
CAUSE_REFUSED = "refused"
CAUSE_UNUSABLE = "unusable"
CAUSE_GONE = "gone"
# An archive only 7-Zip opens, every member of it encrypted. It is kept apart
# from bytes that are not a table because it is not the same statement and the
# user can act on it: those bytes may well hold a good table, and re-packing it
# as a zip makes it importable, where nothing makes an archive with no table in
# it into one.
CAUSE_ENCRYPTED = "encrypted"
# The floor for a cause that cannot be read: a hand-edited file, or a token this
# build does not know. Every entry this build writes states one of the three
# above; this one exists so a label that cannot be made exact never costs the
# record the import protection it is actually there for.
CAUSE_UNKNOWN = "unknown"
BLOCK_CAUSES = frozenset({CAUSE_REFUSED, CAUSE_UNUSABLE, CAUSE_GONE, CAUSE_ENCRYPTED, CAUSE_UNKNOWN})
# The causes that are a statement about whether a table works for a game, which
# is the only kind of record positive compatibility evidence answers to. The
# others are about bytes that never became a table or about the source that
# served them: they refuse those bytes at import, where that is what they mean,
# and they neither invalidate a proven table nor stand between it and a fresh
# proof. One definition, because three surfaces were each deciding it again.
COMPATIBILITY_CAUSES = frozenset({CAUSE_REFUSED, CAUSE_UNKNOWN})


def is_compatibility_failure(entry: "BlockedTable | None") -> bool:
    """Whether this record says the table itself did not work."""
    return entry is not None and entry.cause in COMPATIBILITY_CAUSES


def _dated_by_clearing(entries) -> tuple[str, ...]:
    """The digests whose positive evidence a clear has to leave needing a retest.

    Clearing a record that a table did not work restores every path it stood in
    and must not restore the green it stood over: the success it invalidated was
    earned before the failure, and only a fresh one can speak for the table now.
    A record that arrived without an epoch of its own, which is what a legacy or
    a forward-dated one can be, would otherwise leave that old success valid the
    moment the mark went. Re-dating on the way out is safe for every record,
    because no new proof can be written while one stands.
    """
    return tuple(entry.sha256 for entry in entries if is_compatibility_failure(entry) and entry.sha256)


@dataclass(frozen=True)
class BlockedTable:
    """One table that did not work, and what happened when it was tried.

    Normally that is exact content, and the digest is the identity. The one
    exception is a row whose file the source says it no longer has: nothing was
    downloaded, so there are no bytes to key on, and the provider row is all the
    identity there is. Those entries carry no digest and are recognised only by
    the row, which is exactly how they need to be recognised.
    """

    sha256: str | None
    reason: str
    filename: str | None
    app_id: int | None
    game_name: str | None
    # The version the game's own executable declared when this was recorded. A
    # table that stopped working because the game was updated is the ordinary
    # case, so what it was tried against is the fact that dates the record.
    game_version: str | None
    # The release the source advertised for these exact bytes, which is what
    # search offered them under. A post carries every revision of one table and
    # its attachments share a filename and a title, so this is what tells the
    # record apart from the record of the revision beside it, months later, when
    # the file itself may no longer be on the device to look it up from. None
    # where no source stated one: the `.CT` file's own `CheatEngineTableVersion`
    # is the version of Cheat Engine's table format rather than the table's, and
    # is never used here.
    table_version: str | None
    recorded_at: int
    # `provider:artifact_id` for every provider row these bytes were downloaded
    # from, which is how a search result is recognised when it advertises no
    # digest of its own. Empty for a table imported from a local file.
    origins: tuple[str, ...] = ()
    # What put this entry here: Cheat Engine refused a cheat from it, the bytes
    # were never a usable table, or the source no longer has the file. They are
    # three different things to a user deciding what to do about a row, and only
    # the writer of the record knows which one it is.
    cause: str = CAUSE_UNKNOWN

    def __post_init__(self) -> None:
        # The type's own contract, not just the store's: an entry with neither
        # content nor a row is recognisable by nothing and clearable by hand by
        # nobody, and `key` would raise from a property every read calls.
        if self.sha256 is None and not self.origins:
            raise ValueError("a blocked table needs a SHA-256 or a provider row")

    @property
    def key(self) -> str:
        """What this entry is stored and cleared by.

        The digest where there is one. A row entry is keyed by the row, in a
        space of its own so a provider row can never be mistaken for content.
        """
        return self.sha256 if self.sha256 is not None else f"row:{self.origins[0]}"

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "sha256": self.sha256,
            "reason": self.reason,
            "filename": self.filename,
            "app_id": self.app_id,
            "game_name": self.game_name,
            "game_version": self.game_version,
            "table_version": self.table_version,
            "recorded_at": self.recorded_at,
            "origins": list(self.origins),
            "cause": self.cause,
        }


class BlockedTableError(ValueError):
    """A table this user already marked as not working was offered again."""

    def __init__(self, entry: BlockedTable) -> None:
        super().__init__(
            f"These exact bytes are marked as not working: {entry.reason} "
            "Clear it under Advanced to import it again."
        )
        self.entry = entry


class TableBlocklist:
    """Exact table SHAs marked as not working, with the reason for each."""

    # 4 is the shape every entry has: a digest or a provider row as its
    # identity, why it is here, the game it was tried on and the release it was
    # offered as. 3 is that shape with `table_version` absent, which is already
    # what a record whose source advertised no release carries, so it is read as
    # well and written back as 4 by the next write. That is the whole rule and
    # it is deliberately one shape deep: the shape before this one, differing by
    # a field that may be absent anyway. A file older than that reads as
    # unreadable, which refuses nothing and is cleared by the same press as any
    # other unreadable record.
    #
    # Reading the shape before this one is not politeness, because it is not
    # only the marks that are in this file: it also holds the failure epochs
    # every positive record is checked against, so a file this refused took the
    # proof that a table works with it. `failure_epochs()` raised, the caller
    # read a floor of `None`, and every green on the device became a retest -
    # for a record this version could read perfectly well, one optional field
    # later.
    SCHEMA = 4
    READABLE_SCHEMAS = frozenset({3, 4})

    def __init__(self, path: Path):
        self.path = path

    def list_blocked(self) -> list[BlockedTable]:
        """Newest first: the last thing that failed is what a user is looking for."""
        return sorted(self._load().values(), key=lambda entry: (-entry.recorded_at, entry.key))

    def find(self, sha256: str) -> BlockedTable | None:
        return self._load().get(_sha(sha256))

    def block_row(
        self,
        *,
        origin: str,
        reason: str,
        filename: str | None = None,
        app_id: int | None = None,
        game_name: str | None = None,
        now: int | None = None,
    ) -> BlockedTable:
        """Record a provider row whose file the source says it does not have.

        Keyed by the row because that is the only identity there is: nothing was
        downloaded, so there is no content to key on, and the whole point is
        that this row must not be offered and paid for again. It joins the same
        list the user reads and clears, since to them it is the same statement:
        this one did not work.
        """
        rows = _origins((origin,))
        if not rows:
            raise ValueError("a blocked provider row must name one provider row")
        entry = BlockedTable(
            sha256=None,
            reason=_reason(reason),
            filename=_optional_label(filename, "blocked table filename"),
            app_id=None if app_id is None else _app_id(app_id),
            game_name=_optional_label(game_name, "blocked table game name"),
            game_version=None,
            # A row whose file the source no longer has produced no bytes, so
            # there is no download for a release to have been advertised for.
            table_version=None,
            recorded_at=_timestamp(now),
            origins=rows,
            cause=CAUSE_GONE,
        )
        entries = self._load()
        self._make_room(entries, entry)
        entries[entry.key] = entry
        self._save(entries)
        return entry

    def _make_room(self, entries: dict[str, BlockedTable], entry: BlockedTable) -> None:
        """Bounded like every other durable list, oldest out first.

        Oldest first, but never a decision the user made. A record that a table
        did not work is lifted by one thing, which is the user clearing it, so
        eviction may not become a quiet Clear: enough ordinary download failures
        would otherwise have made a table the user marked selectable again
        without anyone touching it. The records that came from a download or a
        source go first, oldest of those first, and a list holding nothing but
        the user's own decisions refuses the new record instead, in a sentence
        that names the press that makes room.
        """
        if entry.key in entries or len(entries) < MAX_BLOCKED_TABLES:
            return
        ordinary = [item for item in entries.values() if not is_compatibility_failure(item)]
        if not ordinary:
            raise ValueError(
                "the list of tables that did not work is full; clear one under "
                "Advanced, Tables that did not work, to record another"
            )
        oldest = min(ordinary, key=lambda item: (item.recorded_at, item.key))
        entries.pop(oldest.key, None)

    def assert_importable(self, sha256: str) -> None:
        """Raise for bytes already proven not to work, naming the recorded reason."""
        entry = self.find(sha256)
        if entry is not None:
            raise BlockedTableError(entry)

    def add_origins(self, sha256: str, origins: object) -> bool:
        """Merge provider rows into an entry that already exists.

        A table can be recorded before anything knows where it came from: a
        member extracted from an archive is refused by content on a later
        download, and the entry written the first time carries no row at all. It
        stays unrecognisable in search until something adds one, and the refusal
        itself is exactly the moment the row is known.

        Returns whether the record changed. Nothing else about the entry is
        touched: this is not a re-block, so the recorded reason and the day it
        was recorded are the ones the user will read.
        """
        digest = _sha(sha256)
        # Read the whole submission the way every other write path does. Taking
        # the first eight here threw the newest rows away before the retention
        # rule could see them, so a record repaired from a table's stored rows
        # was repaired with the ones it was found through first.
        added = _newest_origins(origins)
        if not added:
            return False
        entries = self._load()
        entry = entries.get(digest)
        if entry is None:
            return False
        # Newest wins once the list is full. Keeping the first eight meant a
        # record that had already met eight posts could never learn a ninth, so
        # the one post just encountered - the only one known to be offering
        # these bytes right now - stayed unrecognisable in search. A row already
        # recorded keeps its original place, so being met again is not treated
        # as news and does not evict anything.
        merged = _newest_origins([*entry.origins, *added])
        if merged == entry.origins:
            return False
        entries[digest] = replace(entry, origins=merged)
        self._save(entries)
        return True

    def name_game(self, key: str, game_name: str) -> bool:
        """Give an entry the game name it was recorded without.

        A record is written at the moment something failed, and for a download
        that failed before any table was chosen that moment is before this game
        has a profile at all: the AppID travelled with the press, the name had
        nowhere to be read from, and the row then said only a file name in a
        list that spans every game. The name exists later, so it is filled in
        later rather than leaving the row unreadable for the life of the record.

        Narrow on purpose. It only fills a blank, never replaces a name the
        record already carries, and touches nothing else: the reason and the day
        it was recorded stay the ones the user will read.
        """
        entries = self._load()
        entry = entries.get(key)
        if entry is None or entry.game_name is not None or entry.app_id is None:
            return False
        named = _optional_label(game_name, "blocked table game name")
        if named is None:
            return False
        entries[key] = replace(entry, game_name=named)
        self._save(entries)
        return True

    def repair(
        self,
        *,
        names: dict[str, str] | None = None,
        origins: dict[str, object] | None = None,
    ) -> dict[str, BlockedTable]:
        """Fill in every blank one read found, and write the store once.

        `name_game` and `add_origins` each load the whole store, apply one fill
        and save the whole store, and saving parses it a second time to carry
        the failure epochs. That is the right shape for a repair that arrives on
        its own, from the press that learned the missing fact, and the wrong one
        for reading the list: the read repairs every row that needs it, and at
        the 512 entries this store allows, answering one read meant a four
        figure number of parses and 512 durable replacements of the same file.

        So the list's repairs come here instead. Same rules, applied in one
        pass: a name only fills a blank and never replaces one the record
        carries, rows merge newest first, nothing else about an entry is
        touched, and the reason and the day stay what the user will read.

        Returns the entries as they now stand, so a caller does not have to read
        the store again to describe what it just repaired, and so what it
        publishes is the record rather than the repair it proposed.
        """
        entries = self._load()
        changed = False
        for key, game_name in (names or {}).items():
            entry = entries.get(key)
            if entry is None or entry.game_name is not None or entry.app_id is None:
                continue
            named = _optional_label(game_name, "blocked table game name")
            if named is None:
                continue
            entries[key] = replace(entry, game_name=named)
            changed = True
        for key, rows in (origins or {}).items():
            entry = entries.get(key)
            if entry is None:
                continue
            added = _newest_origins(rows)
            if not added:
                continue
            merged = _newest_origins([*entry.origins, *added])
            if merged == entry.origins:
                continue
            entries[key] = replace(entry, origins=merged)
            changed = True
        if changed:
            self._save(entries)
        return entries

    def block(
        self,
        *,
        sha256: str,
        reason: str,
        filename: str | None = None,
        app_id: int | None = None,
        game_name: str | None = None,
        game_version: str | None = None,
        table_version: str | None = None,
        origins: object = (),
        cause: str = CAUSE_UNKNOWN,
        now: int | None = None,
    ) -> BlockedTable:
        """Record these exact bytes as not working. Re-blocking replaces the reason.

        A second failure is the more recent evidence and the more useful reason,
        and keeping the first one meant a table that failed for one game kept
        explaining itself in terms of a game the user may no longer own.
        """
        digest = _sha(sha256)
        entry = BlockedTable(
            sha256=digest,
            reason=_reason(reason),
            filename=_optional_label(filename, "blocked table filename"),
            app_id=None if app_id is None else _app_id(app_id),
            game_name=_optional_label(game_name, "blocked table game name"),
            game_version=_optional_label(game_version, "blocked table game version"),
            table_version=_optional_label(table_version, "blocked table release"),
            recorded_at=_timestamp(now),
            origins=_newest_origins(origins),
            cause=_cause(cause),
        )
        entries = self._load()
        held = entries.get(entry.key)
        if held is not None and held.cause == CAUSE_REFUSED and entry.cause != CAUSE_REFUSED:
            # A cheat from these exact bytes ran and did not work, and nothing
            # said about them afterwards undoes that: a stricter parser deciding
            # the file is not a table is a statement about the file, and letting
            # it replace the record would lift the refusal that record carries
            # without the user ever clearing it. The rows the later attempt came
            # through are worth keeping, because that is how the mark is
            # recognised on a source advertising no digest.
            entry = replace(held, origins=_newest_origins([*held.origins, *entry.origins]))
        # The oldest record is the one least likely to still describe an
        # installed game.
        self._make_room(entries, entry)
        entries[entry.key] = entry
        # Only a record about whether the table works dates the evidence about
        # it. An archive nobody could open says nothing about a table that
        # loaded and ran, so it must not retire that proof or block a new one.
        self._save(entries, failed_digests=(digest,) if is_compatibility_failure(entry) else ())
        return entry

    def unblock(self, sha256: str) -> bool:
        """Clear one record, by its digest or by the row a digest-less one holds."""
        key = sha256.strip() if isinstance(sha256, str) and sha256.startswith("row:") else _sha(sha256)
        entries = self._load()
        removed = entries.pop(key, None)
        if removed is None:
            return False
        self._save(entries, failed_digests=_dated_by_clearing((removed,)))
        return True

    def clear(self) -> int:
        """Replace the whole record with an empty one, readable or not.

        This is the repair primitive, not a bulk delete: the one action offered
        for a record that cannot be read is this, and reading it first meant the
        only recovery on offer was the one thing the corrupt state prevented.
        An unreadable record counts as nothing removed, because nothing about
        its contents is known - but it is still replaced.

        Path safety is unchanged: `_save` writes the same managed path through
        the same atomic write, and never follows a link out of it.
        """
        try:
            entries = self._load()
        except (OSError, ValueError):
            self._save({}, reset_epochs=True)
            return 0
        self._save({}, failed_digests=_dated_by_clearing(entries.values()))
        return len(entries)

    def _load(self) -> dict[str, BlockedTable]:
        return self._read_state()[0]

    def _read_state(self):
        raw = load_json(self.path, {"schema": self.SCHEMA, "tables": [], "failure_floor": "0" * 32, "failure_epochs": {}})
        schema = raw.get("schema") if isinstance(raw, dict) else None
        if (
            not isinstance(raw, dict)
            or isinstance(schema, bool)
            or not isinstance(schema, int)
            or schema not in self.READABLE_SCHEMAS
            or set(raw) != {"schema", "tables", "failure_floor", "failure_epochs"}
            or not isinstance(raw.get("tables"), list)
        ):
            raise ValueError("blocked-table state has an unsupported or corrupt schema")
        if len(raw["tables"]) > MAX_BLOCKED_TABLES:
            raise ValueError("blocked-table state contains too many tables")
        entries: dict[str, BlockedTable] = {}
        for item in raw["tables"]:
            entry = _parse(item)
            if entry.key in entries:
                raise ValueError("blocked-table state contains duplicate table SHA-256")
            entries[entry.key] = entry
        floor, epochs = raw["failure_floor"], raw["failure_epochs"]
        valid = lambda token: isinstance(token, str) and re.fullmatch(r"[0-9a-f]{32}", token)
        if not valid(floor) or not isinstance(epochs, dict) or len(epochs) > 4096:
            raise ValueError("invalid failure epochs")
        for digest, epoch in epochs.items():
            _sha(digest)
            if not valid(epoch):
                raise ValueError("invalid failure epoch")
        return entries, floor, epochs

    def failure_epochs(self) -> tuple[str, dict[str, str]]:
        """One strict snapshot, including epochs even when no marks remain."""
        _, floor, epochs = self._read_state()
        return floor, epochs

    def _save(self, entries: dict[str, BlockedTable], *, failed_digests: tuple[str, ...] | list[str] = (),
              reset_epochs: bool = False) -> None:
        try:
            if reset_epochs:
                raise ValueError("reset")
            floor, epochs = self.failure_epochs()
        except (OSError, ValueError):
            floor, epochs = uuid4().hex, {}
        written = set(failed_digests)
        for digest in written:
            epochs[digest] = uuid4().hex
        while len(epochs) > 4096:
            del epochs[next(key for key in epochs if key not in written)]
            # Eviction cannot restore a pre-failure default token.
            floor = uuid4().hex
        payload = {
            "schema": self.SCHEMA,
            "tables": [entry.as_dict() for entry in sorted(entries.values(), key=lambda item: item.key)],
            "failure_floor": floor,
            "failure_epochs": epochs,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, payload)


def _parse(item: Any) -> BlockedTable:
    if not isinstance(item, dict):
        raise ValueError("blocked-table entry must be an object")
    origins = _origins(item.get("origins"))
    digest = item.get("sha256")
    if digest is None and not origins:
        # A record with neither content nor a row is unrecognisable by anything
        # and unclearable by hand, so it is corruption rather than an entry.
        raise ValueError("blocked-table entry has no table SHA-256 and no provider row")
    return BlockedTable(
        sha256=None if digest is None else _sha(digest),
        reason=_reason(item.get("reason")),
        filename=_optional_label(item.get("filename"), "blocked table filename"),
        app_id=None if item.get("app_id") is None else _app_id(item.get("app_id")),
        game_name=_optional_label(item.get("game_name"), "blocked table game name"),
        game_version=_optional_label(item.get("game_version"), "blocked table game version"),
        table_version=_optional_label(item.get("table_version"), "blocked table release"),
        recorded_at=_timestamp(item.get("recorded_at")),
        origins=_origins(item.get("origins", ())),
        cause=_cause(item.get("cause")),
    )


def _origins(value: Any, limit: int = MAX_BLOCKED_ORIGINS) -> tuple[str, ...]:
    """Bounded, de-duplicated `provider:artifact_id` keys, order preserved.

    Provider-described data, so it is validated exactly as strictly as a label
    the same list is rendered beside: an origin that cannot be read is dropped
    on its own rather than costing the record that recognises a bad table.
    """
    if value is None or value == "":
        return ()
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ValueError("blocked table origins must be a list of strings")
    seen: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        text = unicodedata.normalize("NFC", item).strip()
        if not text or len(text.encode("utf-8")) > MAX_ORIGIN_BYTES:
            continue
        try:
            _assert_display_safe(text, "blocked table origin")
        except ValueError:
            continue
        if text not in seen:
            seen.append(text)
        if len(seen) >= limit:
            break
    return tuple(seen)


def _newest_origins(value: object) -> tuple[str, ...]:
    """One retention policy for every path that writes origins: keep the newest.

    De-duplication keeps a row where it was first seen, so the order stays the
    order the rows were recorded in and only the head is dropped. Keeping the
    first eight instead left a table known from many rows carrying the ones it
    was found through years ago, which is exactly the set a search is least
    likely to put in front of the user again.
    """
    return _origins(value, limit=_ORIGIN_SCAN_LIMIT)[-MAX_BLOCKED_ORIGINS:]


def _sha(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("blocked table SHA must be a string")
    digest = value.strip().lower()
    if not _SHA_RE.fullmatch(digest):
        raise ValueError("blocked table SHA must be a 64-character hexadecimal digest")
    return digest


def _reason(value: Any) -> str:
    """One bounded, single-line, display-safe sentence.

    The reason is assembled from what Cheat Engine and the table said, so it is
    untrusted text that is later rendered in the Decky UI beside a filename.
    Invisible and bidirectional formatting is refused for the same reason a
    table's own control labels refuse it: it can reorder what is on screen.
    """
    if not isinstance(value, str):
        raise ValueError("blocked table reason must be a string")
    text = unicodedata.normalize("NFC", value).strip()
    if not text or utf8_len(text, "blocked table reason") > MAX_REASON_BYTES:
        raise ValueError(f"blocked table reason must contain 1..{MAX_REASON_BYTES} UTF-8 bytes")
    _assert_display_safe(text, "blocked table reason")
    return text


def _cause(value: Any) -> str:
    """One of the known causes, or the floor.

    Fail-soft on purpose, unlike every other field here: the cause decides which
    two words a chip says, and a record whose cause cannot be read is still a
    record of a table that does not work. Refusing it would drop the protection
    to keep a label honest.
    """
    return value if isinstance(value, str) and value in BLOCK_CAUSES else CAUSE_UNKNOWN


def _optional_label(value: Any, field: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string or null")
    text = unicodedata.normalize("NFC", value).strip()
    if not text or utf8_len(text, field) > MAX_LABEL_BYTES:
        raise ValueError(f"{field} must contain 1..{MAX_LABEL_BYTES} UTF-8 bytes")
    _assert_display_safe(text, field)
    return text


def _assert_display_safe(text: str, field: str) -> None:
    for ch in text:
        if unicodedata.category(ch) in {"Cc", "Cf"}:
            raise ValueError(f"{field} contains control or formatting characters")
        if unicodedata.bidirectional(ch) in {"RLE", "LRE", "RLO", "LRO", "RLI", "LRI", "FSI", "PDI", "PDF"}:
            raise ValueError(f"{field} contains bidirectional control characters")


def _app_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > 0xFFFFFFFF:
        raise ValueError("AppID must be an integer between 1 and 4294967295")
    return value


def _timestamp(value: Any) -> int:
    if value is None:
        return int(time.time())
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 0xFFFFFFFFFF:
        raise ValueError("blocked table timestamp must be a non-negative bounded integer")
    return value
