from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
import os
import re
import uuid
import unicodedata
from typing import Any

from .atomic import DurabilityUnknownError, atomic_write_json, durable_rename, load_json
from .text import utf8_len

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_PROCESS_RE = re.compile(r"^[^\\/:*?\"<>|\x00-\x1f]{1,255}\.exe$", re.IGNORECASE)
_MAX_HISTORY_TABLES = 256
_MAX_PINNED_CONTROLS = 128
_MAX_STARTUP_CONTROLS = 1024
_MAX_REMEMBERED_CONTROLS = 1024
_MAX_CONFIGURED_VALUES = 1024
MAX_EFFECTIVE_STARTUP_ACTIONS = 2048
# Keep room for the full bounded preference history plus additional downloaded/imported
# associations. Current + previous + all history entries are never eviction candidates.
_MAX_TABLE_LIBRARY = _MAX_HISTORY_TABLES * 2
_MAX_PROFILES = 10_000


@dataclass(frozen=True)
class StartupPreference:
    record_id: int
    active: bool | None = None
    value: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ConfiguredValue:
    record_id: int
    value: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class TablePreferences:
    startup: tuple[StartupPreference, ...] = ()
    pinned: tuple[int, ...] = ()
    execution_consent: bool = False
    remembered: tuple[StartupPreference, ...] = ()
    configured_values: tuple[ConfiguredValue, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "startup": [item.as_dict() for item in self.startup],
            "pinned": list(self.pinned),
            "execution_consent": self.execution_consent,
            "remembered": [item.as_dict() for item in self.remembered],
            "configured_values": [item.as_dict() for item in self.configured_values],
        }


@dataclass
class GameProfile:
    app_id: int
    name: str
    is_shortcut: bool
    table_sha256: str | None = None
    target_process: str | None = None
    execution_consent_sha256: str | None = None
    startup: list[StartupPreference] = field(default_factory=list)
    pinned: list[int] = field(default_factory=list)
    previous_table_sha256: str | None = None
    table_history: dict[str, TablePreferences] = field(default_factory=dict)
    table_library: list[str] = field(default_factory=list)
    autoload_enabled: bool = False
    remembered: list[StartupPreference] = field(default_factory=list)
    configured_values: list[ConfiguredValue] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "app_id": self.app_id,
            "name": self.name,
            "is_shortcut": self.is_shortcut,
            "table_sha256": self.table_sha256,
            "target_process": self.target_process,
            "execution_consent_sha256": self.execution_consent_sha256,
            "startup": [item.as_dict() for item in self.startup],
            "pinned": list(self.pinned),
            "previous_table_sha256": self.previous_table_sha256,
            "table_history": {digest: self.table_history[digest].as_dict() for digest in sorted(self.table_history)},
            "table_library": list(self.table_library),
            "autoload_enabled": self.autoload_enabled,
            "remembered": [item.as_dict() for item in self.remembered],
            "configured_values": [item.as_dict() for item in self.configured_values],
        }


class ProfileStore:
    SCHEMA = 5

    def __init__(self, path: Path):
        self.path = path

    def list_profiles(self) -> list[GameProfile]:
        return sorted(self._load().values(), key=lambda profile: (profile.name.casefold(), profile.app_id))

    def get(self, app_id: int) -> GameProfile | None:
        app_id = _app_id(app_id)
        return self._load().get(app_id)

    def upsert(
        self,
        *,
        app_id: int,
        name: str,
        is_shortcut: bool,
        table_sha256: str | None,
        target_process: str | None,
    ) -> GameProfile:
        app_id = _app_id(app_id)
        name = _name(name)
        if not isinstance(is_shortcut, bool):
            raise ValueError("is_shortcut must be a boolean")
        digest = _optional_sha(table_sha256)
        process = _optional_process(target_process)
        profiles = self._load()
        existing = profiles.get(app_id)

        startup: list[StartupPreference] = []
        pinned: list[int] = []
        consent: str | None = None
        previous: str | None = None
        history: dict[str, TablePreferences] = {}
        table_library: list[str] = []
        autoload_enabled = False
        remembered: list[StartupPreference] = []
        configured_values: list[ConfiguredValue] = []

        if existing:
            identity_changed = existing.is_shortcut != is_shortcut
            if not identity_changed:
                history = dict(existing.table_history)
                previous = existing.previous_table_sha256
                table_library = list(existing.table_library)
                autoload_enabled = existing.autoload_enabled
                if existing.table_sha256 == digest:
                    startup = list(existing.startup)
                    pinned = list(existing.pinned)
                    consent = existing.execution_consent_sha256
                    remembered = list(existing.remembered)
                    configured_values = list(existing.configured_values)
                else:
                    if existing.table_sha256 is not None:
                        history[existing.table_sha256] = TablePreferences(
                            tuple(existing.startup),
                            tuple(existing.pinned),
                            existing.execution_consent_sha256 == existing.table_sha256,
                            tuple(existing.remembered),
                            tuple(existing.configured_values),
                        )
                        previous = existing.table_sha256
                    if digest is not None:
                        restored = history.pop(digest, None)
                        if restored is not None:
                            startup = list(restored.startup)
                            pinned = list(restored.pinned)
                            consent = digest if restored.execution_consent else None
                            remembered = list(restored.remembered)
                            configured_values = list(restored.configured_values)

        # Selecting the table this profile would go back to leaves nothing to
        # go back to. `previous` deliberately survives a cleared selection, so
        # that a table taken away can be chosen again without hunting for it,
        # and choosing exactly that one then made the pointer name the current
        # table, which is not a previous selection and is refused on the way to
        # disk. Answering the refusal dialog with "Stop using it" clears the
        # selection and records that table as the previous one, so clearing the
        # mark under Advanced and loading the same file again is the ordinary
        # way to reach it: the user was told their own table could not be
        # selected, with no network and nothing else to select instead.
        if previous is not None and previous == digest:
            previous = None

        if digest is not None and digest not in table_library:
            table_library.append(digest)
        # A retained previous selection always reaches `history` above, so the
        # library needs no separate carry-over for it. Deriving associations from
        # `history` alone also keeps a Steam/shortcut identity change a real reset
        # instead of leaving the discarded profile's table associated.
        for historical_digest in history:
            if historical_digest not in table_library:
                table_library.append(historical_digest)
        # Keep the immediately previous table reversible even when old table
        # selections have filled the bounded preference cache. Dict insertion
        # order is our LRU order: restore moves an entry out and revisiting a
        # current table appends its predecessor at the end. Bound history before
        # table-library eviction so every surviving history SHA can remain a
        # required association.
        while len(history) > _MAX_HISTORY_TABLES:
            oldest = next(iter(history))
            if oldest == previous and len(history) > 1:
                oldest = next(key for key in history if key != previous)
            del history[oldest]

        while len(table_library) > _MAX_TABLE_LIBRARY:
            protected = {item for item in (digest, previous, *history.keys()) if item is not None}
            drop = next((item for item in table_library if item not in protected), None)
            if drop is None:
                raise ValueError("profile table library cannot be bounded without dropping current or historical table state")
            table_library.remove(drop)
        if existing is None and len(profiles) >= _MAX_PROFILES:
            raise ValueError("profiles state has reached the supported profile limit")
        profile = GameProfile(
            app_id=app_id,
            name=name,
            is_shortcut=is_shortcut,
            table_sha256=digest,
            target_process=process,
            execution_consent_sha256=consent,
            startup=startup,
            pinned=pinned,
            previous_table_sha256=previous,
            table_history=history,
            table_library=table_library,
            autoload_enabled=autoload_enabled,
            remembered=remembered,
            configured_values=configured_values,
        )
        profiles[app_id] = profile
        self._save(profiles)
        return profile

    def set_execution_consent(self, *, app_id: int, table_sha256: str, consent: bool) -> GameProfile:
        app_id = _app_id(app_id)
        digest = _sha(table_sha256)
        if not isinstance(consent, bool):
            raise ValueError("consent must be boolean")
        profiles = self._load()
        profile = _current_profile(profiles, app_id, digest, "execution consent")
        profile.execution_consent_sha256 = digest if consent else None
        self._save(profiles)
        return profile

    def revoke_table(self, *, app_id: int, table_sha256: str) -> GameProfile:
        """Withdraw and detach one exact selection in one durable save."""
        app_id = _app_id(app_id)
        digest = _sha(table_sha256)
        profiles = self._load()
        profile = profiles.get(app_id)
        if profile is None:
            raise ValueError("game profile does not exist")
        if profile.table_sha256 is None:
            previous = profile.table_history.get(digest)
            if profile.previous_table_sha256 == digest and previous is not None and not previous.execution_consent and not profile.autoload_enabled:
                return profile
        profile = _current_profile(profiles, app_id, digest, "table revocation")
        profile.execution_consent_sha256 = None
        profile.autoload_enabled = False
        profile.table_history[digest] = TablePreferences(
            tuple(profile.startup), tuple(profile.pinned), False, tuple(profile.remembered), tuple(profile.configured_values),
        )
        profile.previous_table_sha256 = digest
        profile.table_sha256 = None
        profile.startup = []
        profile.pinned = []
        profile.remembered = []
        profile.configured_values = []
        while len(profile.table_history) > _MAX_HISTORY_TABLES:
            oldest = next(key for key in profile.table_history if key != digest)
            del profile.table_history[oldest]
        self._save(profiles)
        return profile

    def set_startup(
        self,
        *,
        app_id: int,
        table_sha256: str,
        record_id: int,
        active: bool | None,
        value: str | None,
    ) -> GameProfile:
        app_id = _app_id(app_id)
        digest = _sha(table_sha256)
        record_id = _record_id(record_id)
        if active is not None and not isinstance(active, bool):
            raise ValueError("active must be boolean or null")
        if value is not None:
            if not isinstance(value, str):
                raise ValueError("startup value must be a string or null")
            if utf8_len(value, "startup value") > 4096:
                raise ValueError("startup value is too long")
        if active is None and value is None:
            raise ValueError("at least one startup setting must be provided")
        profiles = self._load()
        profile = _current_profile(profiles, app_id, digest, "startup settings")
        by_id = {item.record_id: item for item in profile.startup}
        is_new_startup = record_id not in by_id
        by_id[record_id] = StartupPreference(record_id, active, value)
        if len(by_id) > _MAX_STARTUP_CONTROLS:
            raise ValueError("too many startup controls for one table")
        profile.startup = sorted(by_id.values(), key=lambda item: item.record_id)
        _validate_effective_startup_budget(profile.startup, profile.remembered, profile.configured_values)
        # A newly selected startup record is useful as a runtime quick control, so pin it
        # by default. Do this only on first creation: an explicit later unpin must remain
        # respected when the startup value/state is edited. Hitting the pin display limit
        # must not make an otherwise valid startup action fail.
        if is_new_startup and record_id not in profile.pinned and len(profile.pinned) < _MAX_PINNED_CONTROLS:
            profile.pinned = sorted([*profile.pinned, record_id])
        self._save(profiles)
        return profile

    def clear_startup(self, *, app_id: int, table_sha256: str, record_id: int | None = None) -> GameProfile:
        app_id = _app_id(app_id)
        digest = _sha(table_sha256)
        profiles = self._load()
        profile = _current_profile(profiles, app_id, digest, "startup settings")
        if record_id is None:
            profile.startup = []
        else:
            rid = _record_id(record_id)
            profile.startup = [item for item in profile.startup if item.record_id != rid]
        self._save(profiles)
        return profile

    def set_pinned(self, *, app_id: int, table_sha256: str, record_id: int, pinned: bool) -> GameProfile:
        app_id = _app_id(app_id)
        digest = _sha(table_sha256)
        rid = _record_id(record_id)
        if not isinstance(pinned, bool):
            raise ValueError("pinned must be boolean")
        profiles = self._load()
        profile = _current_profile(profiles, app_id, digest, "pinned controls")
        values = set(profile.pinned)
        if pinned:
            values.add(rid)
        else:
            values.discard(rid)
        if len(values) > _MAX_PINNED_CONTROLS:
            raise ValueError("too many pinned controls for one table")
        profile.pinned = sorted(values)
        self._save(profiles)
        return profile

    def clear_pinned(self, *, app_id: int, table_sha256: str) -> GameProfile:
        app_id = _app_id(app_id)
        digest = _sha(table_sha256)
        profiles = self._load()
        profile = _current_profile(profiles, app_id, digest, "pinned controls")
        profile.pinned = []
        self._save(profiles)
        return profile

    def associate_table(self, *, app_id: int, table_sha256: str) -> GameProfile:
        app_id = _app_id(app_id)
        digest = _sha(table_sha256)
        profiles = self._load()
        profile = profiles.get(app_id)
        if profile is None:
            raise ValueError("game profile does not exist")
        if digest not in profile.table_library:
            profile.table_library.append(digest)
        while len(profile.table_library) > _MAX_TABLE_LIBRARY:
            protected = {
                item for item in (profile.table_sha256, profile.previous_table_sha256, *profile.table_history.keys())
                if item is not None
            }
            drop = next((item for item in profile.table_library if item not in protected), None)
            if drop is None:
                raise ValueError("profile table library cannot be bounded without dropping current or historical table state")
            profile.table_library.remove(drop)
        self._save(profiles)
        return profile

    def set_autoload(self, *, app_id: int, table_sha256: str, enabled: bool) -> GameProfile:
        app_id = _app_id(app_id)
        digest = _sha(table_sha256)
        if not isinstance(enabled, bool):
            raise ValueError("autoload enabled must be a boolean")
        profiles = self._load()
        profile = _current_profile(profiles, app_id, digest, "autoload settings")
        profile.autoload_enabled = enabled
        self._save(profiles)
        return profile

    def set_remembered(self, *, app_id: int, table_sha256: str, states: list[StartupPreference]) -> GameProfile:
        app_id = _app_id(app_id)
        digest = _sha(table_sha256)
        if not isinstance(states, list):
            raise ValueError("remembered cheat state must be a list")
        normalized = _parse_preferences([item.as_dict() if isinstance(item, StartupPreference) else item for item in states],
                                        max_items=_MAX_REMEMBERED_CONTROLS, label="remembered cheat state")
        profiles = self._load()
        profile = _current_profile(profiles, app_id, digest, "remembered cheat state")
        profile.remembered = normalized
        _validate_effective_startup_budget(profile.startup, profile.remembered, profile.configured_values)
        self._save(profiles)
        return profile

    def set_configured_values(
        self, *, app_id: int, table_sha256: str, values: list[ConfiguredValue]
    ) -> GameProfile:
        app_id = _app_id(app_id)
        digest = _sha(table_sha256)
        if not isinstance(values, list):
            raise ValueError("configured values must be a list")
        normalized = _parse_configured_values([
            item.as_dict() if isinstance(item, ConfiguredValue) else item for item in values
        ])
        profiles = self._load()
        profile = _current_profile(profiles, app_id, digest, "configured values")
        profile.configured_values = normalized
        _validate_effective_startup_budget(profile.startup, profile.remembered, profile.configured_values)
        self._save(profiles)
        return profile

    def delete(self, app_id: int) -> bool:
        app_id = _app_id(app_id)
        profiles = self._load()
        existed = profiles.pop(app_id, None) is not None
        if existed:
            self._save(profiles)
        return existed

    def state_reason(self) -> str | None:
        """Report why this store cannot be read, if it cannot."""
        try:
            self._load()
        except ValueError as exc:
            return str(exc)[:512]
        return None

    def quarantine_corrupt_state(self) -> dict[str, object]:
        """Move an unreadable profile store aside and start a clean one.

        Strict parsing correctly detects a truncated, malformed or unsupported
        `profiles.json`, but every read and every write begins from the same
        parse, so one corrupt file left the plugin visibly alive while no game
        could be configured, associated, consented, armed or deleted again -
        and even Cheat Engine re-import enumerates profiles first. The corrupt
        file is preserved as evidence rather than deleted; the caller proves
        first that nothing owns the identities it holds.
        """
        reason = self.state_reason()
        if reason is None:
            raise ValueError("profile state is readable; it does not need repair")
        quarantine = self.path.with_name(f"{self.path.name}.invalid-{uuid.uuid4().hex}")
        try:
            durable_rename(self.path, quarantine)
        except DurabilityUnknownError:
            raise
        except FileNotFoundError as exc:
            raise ValueError("the unreadable profile state disappeared; refresh and try again") from exc
        except OSError as exc:
            raise ValueError(f"failed to quarantine unreadable profile state: {exc}") from exc
        try:
            self._save({})
        except (OSError, ValueError) as exc:
            raise DurabilityUnknownError(
                "profile authority was quarantined, but the empty replacement could not be committed; refresh before retrying"
            ) from exc
        return {"discarded": True, "quarantined": quarantine.name, "reason": reason}

    def _load(self) -> dict[int, GameProfile]:
        raw = load_json(self.path, {"schema": self.SCHEMA, "profiles": []})
        schema = raw.get("schema") if isinstance(raw, dict) else None
        if (
            not isinstance(raw, dict)
            or isinstance(schema, bool)
            or not isinstance(schema, int)
            or schema not in (1, 2, 3, 4, self.SCHEMA)
            or not isinstance(raw.get("profiles"), list)
        ):
            raise ValueError("profiles state has an unsupported or corrupt schema")
        if len(raw["profiles"]) > _MAX_PROFILES:
            raise ValueError("profiles state contains too many profiles")
        profiles: dict[int, GameProfile] = {}
        for item in raw["profiles"]:
            profile = _parse_profile(item, schema=schema)
            if profile.app_id in profiles:
                raise ValueError("profiles state contains duplicate AppID")
            profiles[profile.app_id] = profile
        return profiles

    def _save(self, profiles: dict[int, GameProfile]) -> None:
        if len(profiles) > _MAX_PROFILES:
            raise ValueError("profiles state contains too many profiles")
        serialized: list[dict[str, object]] = []
        for key in sorted(profiles):
            profile = profiles[key]
            if key != profile.app_id:
                raise ValueError("profile map key does not match profile AppID")
            raw = profile.as_dict()
            # Re-parse the exact serialized representation before persistence so
            # direct dataclass mutation cannot create state that succeeds once and
            # becomes unreadable on the following load.
            _parse_profile(raw, schema=self.SCHEMA)
            serialized.append(raw)
        atomic_write_json(self.path, {"schema": self.SCHEMA, "profiles": serialized})


def _effective_preferences(
    startup: list[StartupPreference] | tuple[StartupPreference, ...],
    remembered: list[StartupPreference] | tuple[StartupPreference, ...],
    configured_values: list[ConfiguredValue] | tuple[ConfiguredValue, ...] = (),
) -> list[StartupPreference]:
    by_id = {item.record_id: item for item in startup}
    for item in remembered:
        base = by_id.get(item.record_id)
        by_id[item.record_id] = StartupPreference(
            item.record_id,
            item.active if item.active is not None else (base.active if base is not None else None),
            item.value if item.value is not None else (base.value if base is not None else None),
        )
    for item in configured_values:
        base = by_id.get(item.record_id)
        active = base.active if base is not None else None
        if active is False:
            # An explicitly switched-off cheat keeps its configured value for
            # whenever it is switched back on; it does not become a startup
            # write. Overlaying it regardless wrote game memory for a cheat the
            # user had deliberately disabled, and for a dynamic child whose
            # enclosing script is off it made the value unresolvable and failed
            # the whole Auto-load.
            continue
        by_id[item.record_id] = StartupPreference(item.record_id, active, item.value)
    return [by_id[key] for key in sorted(by_id)]


def _validate_effective_startup_budget(
    startup: list[StartupPreference] | tuple[StartupPreference, ...],
    remembered: list[StartupPreference] | tuple[StartupPreference, ...],
    configured_values: list[ConfiguredValue] | tuple[ConfiguredValue, ...] = (),
) -> None:
    action_count = sum(
        int(item.active is not None) + int(item.value is not None)
        for item in _effective_preferences(startup, remembered, configured_values)
    )
    if action_count > MAX_EFFECTIVE_STARTUP_ACTIONS:
        raise ValueError(f"effective startup state exceeds {MAX_EFFECTIVE_STARTUP_ACTIONS} action limit")


def _current_profile(profiles: dict[int, GameProfile], app_id: int, digest: str, purpose: str) -> GameProfile:
    profile = profiles.get(app_id)
    if profile is None:
        raise ValueError("game profile does not exist")
    if profile.table_sha256 != digest:
        raise ValueError(f"{purpose} are bound to the profile's exact selected table SHA")
    return profile


def _parse_profile(raw: Any, *, schema: int) -> GameProfile:
    if not isinstance(raw, dict):
        raise ValueError("profile entry must be an object")
    base_keys = {
        "app_id", "name", "is_shortcut", "table_sha256", "target_process",
        "execution_consent_sha256", "startup",
    }
    allowed = set(base_keys)
    if schema >= 2:
        # Legacy key from the removed Steam launch-option integration. No shipped
        # surface ever recorded one, so it is always null on disk: accept that,
        # discard it, and stop writing it back. A non-null value would mean this
        # profile owns Steam state this build can no longer restore, so say so
        # instead of silently dropping the record.
        allowed.add("launch_ownership")
    if schema >= 3:
        allowed.update({"pinned", "previous_table_sha256", "table_history"})
    if schema >= 4:
        allowed.update({"table_library", "autoload_enabled", "remembered"})
    if schema >= 5:
        allowed.add("configured_values")
    if set(raw) - allowed:
        raise ValueError("profile entry contains unknown fields for its schema")
    app_id = _app_id(raw.get("app_id"))
    name = _name(raw.get("name"))
    is_shortcut = raw.get("is_shortcut")
    if not isinstance(is_shortcut, bool):
        raise ValueError("profile is_shortcut must be boolean")
    digest = _optional_sha(raw.get("table_sha256"))
    process = _optional_process(raw.get("target_process"))
    consent = _optional_sha(raw.get("execution_consent_sha256"))
    if consent is not None and consent != digest:
        raise ValueError("execution consent SHA must match the selected table SHA")
    startup = _parse_startup(raw.get("startup", []))
    pinned = _parse_pinned(raw.get("pinned", [])) if schema >= 3 else []
    previous = _optional_sha(raw.get("previous_table_sha256")) if schema >= 3 else None
    history = _parse_history(raw.get("table_history", {}), schema=schema) if schema >= 3 else {}
    if schema >= 4:
        table_library = _parse_table_library(raw.get("table_library", []))
    else:
        # Schema 1-3 had no explicit per-game library. Derive the association
        # from the exact SHA identities already persisted by those schemas.
        table_library = []
        for historical_digest in (digest, previous, *history.keys()):
            if historical_digest is not None and historical_digest not in table_library:
                table_library.append(historical_digest)
    autoload_enabled = raw.get("autoload_enabled", False) if schema >= 4 else False
    if not isinstance(autoload_enabled, bool):
        raise ValueError("profile autoload_enabled must be a boolean")
    remembered = _parse_preferences(raw.get("remembered", []), max_items=_MAX_REMEMBERED_CONTROLS, label="remembered cheat state") if schema >= 4 else []
    if schema >= 5:
        configured_values = _parse_configured_values(raw.get("configured_values", []))
    else:
        # Schema 4 stored user-entered values inside the live remembered state.
        # Preserve them as exact-table configuration during the schema rewrite;
        # later live reconciliation may change remembered without touching this.
        configured_values = _configured_from_remembered(remembered)
    if digest is not None and digest in history:
        raise ValueError("current table SHA must not also exist in table preference history")
    if previous is not None and previous == digest:
        raise ValueError("previous table SHA must differ from the current table SHA")
    if schema >= 4:
        required_library = {item for item in (digest, previous, *history.keys()) if item is not None}
        if not required_library.issubset(table_library):
            raise ValueError("profile table_library is missing a current, previous, or historical exact table SHA")

    _validate_effective_startup_budget(startup, remembered, configured_values)

    if raw.get("launch_ownership") is not None:
        raise ValueError(
            "profile records Steam launch ownership from a removed feature; "
            "restore the game's Steam launch options and clear that record"
        )

    return GameProfile(
        app_id=app_id,
        name=name,
        is_shortcut=is_shortcut,
        table_sha256=digest,
        target_process=process,
        execution_consent_sha256=consent,
        startup=startup,
        pinned=pinned,
        previous_table_sha256=previous,
        table_history=history,
        table_library=table_library,
        autoload_enabled=autoload_enabled,
        remembered=remembered,
        configured_values=configured_values,
    )


def _parse_startup(raw: Any) -> list[StartupPreference]:
    return _parse_preferences(raw, max_items=_MAX_STARTUP_CONTROLS, label="profile startup")


def _parse_preferences(raw: Any, *, max_items: int, label: str) -> list[StartupPreference]:
    if not isinstance(raw, list) or len(raw) > max_items:
        raise ValueError(f"{label} must be a bounded list")
    preferences: list[StartupPreference] = []
    seen: set[int] = set()
    for item in raw:
        if not isinstance(item, dict) or set(item) - {"record_id", "active", "value"}:
            raise ValueError(f"{label} preference must be an object")
        rid = _record_id(item.get("record_id"))
        if rid in seen:
            raise ValueError(f"duplicate {label} MemoryRecord ID")
        seen.add(rid)
        active = item.get("active")
        value = item.get("value")
        if active is not None and not isinstance(active, bool):
            raise ValueError(f"{label} active must be boolean or null")
        if value is not None:
            if not isinstance(value, str):
                raise ValueError(f"{label} value must be string or null")
            if utf8_len(value, f"{label} value") > 4096:
                raise ValueError(f"{label} value is too long")
        if active is None and value is None:
            raise ValueError(f"empty {label} preference is invalid")
        preferences.append(StartupPreference(rid, active, value))
    preferences.sort(key=lambda item: item.record_id)
    return preferences


def _parse_configured_values(raw: Any) -> list[ConfiguredValue]:
    if not isinstance(raw, list) or len(raw) > _MAX_CONFIGURED_VALUES:
        raise ValueError("configured values must be a bounded list")
    values: list[ConfiguredValue] = []
    seen: set[int] = set()
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"record_id", "value"}:
            raise ValueError("configured value must contain record_id and value")
        rid = _record_id(item.get("record_id"))
        if rid in seen:
            raise ValueError("duplicate configured-value MemoryRecord ID")
        seen.add(rid)
        value = item.get("value")
        if not isinstance(value, str):
            raise ValueError("configured value must be a string")
        if utf8_len(value, "configured value") > 4096:
            raise ValueError("configured value is too long")
        if not is_configurable_value(value):
            raise ValueError("configured value must not be blank or Cheat Engine's unreadable placeholder")
        values.append(ConfiguredValue(rid, value))
    values.sort(key=lambda item: item.record_id)
    return values


def is_configurable_value(value: str) -> bool:
    """A value worth storing as this table's own configuration.

    Cheat Engine answers ``??`` for an address it cannot read yet, and a cleared
    field is blank. Neither is a user choice, and storing one would make the
    next session write a placeholder over whatever the table actually holds.
    """
    text = value.strip()
    return bool(text) and text != "??"


def _configured_from_remembered(
    remembered: list[StartupPreference] | tuple[StartupPreference, ...],
) -> list[ConfiguredValue]:
    return [
        ConfiguredValue(item.record_id, item.value)
        for item in remembered
        if item.value is not None and is_configurable_value(item.value)
    ]


def _parse_pinned(raw: Any) -> list[int]:
    if not isinstance(raw, list) or len(raw) > _MAX_PINNED_CONTROLS:
        raise ValueError("profile pinned controls must be a bounded list")
    values = [_record_id(item) for item in raw]
    if len(values) != len(set(values)):
        raise ValueError("profile pinned controls contain duplicate MemoryRecord IDs")
    return sorted(values)


def _parse_history(raw: Any, *, schema: int) -> dict[str, TablePreferences]:
    if not isinstance(raw, dict) or len(raw) > _MAX_HISTORY_TABLES:
        raise ValueError("profile table preference history must be a bounded object")
    result: dict[str, TablePreferences] = {}
    expected = {"startup", "pinned", "execution_consent"}
    if schema >= 4:
        expected.add("remembered")
    if schema >= 5:
        expected.add("configured_values")
    for key, value in raw.items():
        digest = _sha(key)
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("table preference history entry is malformed")
        consent = value.get("execution_consent")
        if not isinstance(consent, bool):
            raise ValueError("table preference execution_consent must be boolean")
        startup = tuple(_parse_startup(value.get("startup")))
        remembered = tuple(_parse_preferences(
            value.get("remembered", []), max_items=_MAX_REMEMBERED_CONTROLS, label="remembered cheat state"
        ))
        if schema >= 5:
            configured_values = tuple(_parse_configured_values(value.get("configured_values", [])))
        else:
            configured_values = tuple(_configured_from_remembered(remembered))
        _validate_effective_startup_budget(startup, remembered, configured_values)
        result[digest] = TablePreferences(
            startup,
            tuple(_parse_pinned(value.get("pinned"))),
            consent,
            remembered,
            configured_values,
        )
    return result


def _parse_table_library(raw: Any) -> list[str]:
    if not isinstance(raw, list) or len(raw) > _MAX_TABLE_LIBRARY:
        raise ValueError("profile table_library must be a bounded list")
    values = [_sha(item) for item in raw]
    if len(values) != len(set(values)):
        raise ValueError("profile table_library contains duplicate table SHA")
    return values


def _app_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > 0xFFFFFFFF:
        raise ValueError("AppID must be an integer between 1 and 4294967295")
    return value


def _record_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 0x7FFFFFFF:
        raise ValueError("MemoryRecord ID must be an integer between 0 and 2147483647")
    return value


def _name(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("profile name must be a string")
    text = unicodedata.normalize("NFC", value).strip()
    if not text or utf8_len(text, "profile name") > 1024:
        raise ValueError("profile name must contain 1..1024 UTF-8 bytes")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        raise ValueError("profile name contains control characters")
    if any(unicodedata.bidirectional(ch) in {"RLE", "LRE", "RLO", "LRO", "PDF", "RLI", "LRI", "FSI", "PDI"} for ch in text):
        raise ValueError("profile name contains bidirectional control characters")
    return text


def _sha(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("table SHA must be a string")
    digest = value.strip().lower()
    if not _SHA_RE.fullmatch(digest):
        raise ValueError("table SHA must be a 64-character hexadecimal digest")
    return digest


def _optional_sha(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return _sha(value)


def _optional_process(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("target process must be a string or null")
    text = unicodedata.normalize("NFC", value.strip())
    if (
        not _PROCESS_RE.fullmatch(text)
        or "/" in text
        or "\\" in text
        or any(unicodedata.category(ch) in {"Cc", "Cf"} for ch in text)
        or any(unicodedata.bidirectional(ch) in {"RLE", "LRE", "RLO", "LRO", "PDF", "RLI", "LRI", "FSI", "PDI"} for ch in text)
    ):
        raise ValueError("target process must be an unambiguous basename ending in .exe")
    return text
