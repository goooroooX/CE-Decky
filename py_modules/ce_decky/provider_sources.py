"""Which table sources this user actually wants CE Decky to use.

Every registered provider is on by default and stays on until the user says
otherwise, so what is recorded here is the set they switched **off**, never the
set they left on. That direction is the whole design: the registry grows, and a
file listing the enabled sources would silently switch every source added by a
later version off for everybody who already had this file, with nothing on any
screen saying why a source stopped answering. A file listing the refusals says
exactly what the user decided and nothing about the providers they never had an
opinion about.

The record lives under `state/` rather than beside the provider diagnostics in
`cache/`, because the cache is offered for deletion as "safe to delete; it is
rebuilt on the next search" and this cannot be rebuilt from anything. It is a
preference and not a trust decision: nothing here authorizes a provider, and a
table already downloaded from a source that has since been switched off keeps
working exactly as it did.

An unreadable record therefore means the user's choice is unknown, which is
deliberately not the same as "everything is off": that would leave the plugin
unable to find anything with no visible cause, so an unreadable file reads as no
refusals at all, exactly as an unreadable blocklist refuses nothing. The reason
is carried out to Advanced, the diagnostics snapshot and the support bundle
instead of being swallowed, and Advanced offers to replace the file.
"""

from __future__ import annotations

from pathlib import Path
import time
from typing import Any

from .atomic import atomic_write_json, load_json
from .providers import normalized_provider_id

# A provider the user turned off keeps its entry for as long as this file
# exists, including while it is absent from the registry, so that a source
# removed for one version and restored in the next does not come back on by
# itself. The bound is well above the registry's own ceiling for exactly that
# reason, and it is still a bound: this file is not a place to accumulate.
MAX_DISABLED_PROVIDERS = 64


class ProviderSourceSelection:
    """The provider IDs this user switched off, and nothing else."""

    SCHEMA = 1

    def __init__(self, path: Path) -> None:
        self.path = path

    def disabled(self) -> frozenset[str]:
        """The switched-off provider IDs. Raises if the record cannot be read."""
        return frozenset(self._load()[0])

    def is_enabled(self, provider_id: str) -> bool:
        return normalized_provider_id(provider_id) not in self.disabled()

    def set_enabled(self, provider_id: str, enabled: bool) -> bool:
        """Record one provider as on or off. Returns whether anything changed.

        Switching a provider back on removes its entry rather than recording an
        approval, so the file only ever holds refusals and an untouched provider
        never appears in it at all.
        """
        provider = normalized_provider_id(provider_id)
        if not isinstance(enabled, bool):
            raise ValueError("provider enablement must be boolean")
        disabled, _ = self._load()
        if enabled:
            if provider not in disabled:
                return False
            disabled = [item for item in disabled if item != provider]
        else:
            if provider in disabled:
                return False
            if len(disabled) >= MAX_DISABLED_PROVIDERS:
                raise ValueError("too many switched-off providers are recorded")
            disabled = [*disabled, provider]
        self._save(disabled)
        return True

    def clear(self) -> int:
        """Replace the whole record with an empty one, readable or not.

        The repair primitive, for the same reason the blocklist has one: the
        single action offered for a record that cannot be read is this, and
        reading it first would make the only recovery on offer depend on the one
        thing the corrupt state prevents. An unreadable record counts as nothing
        removed, because nothing about its contents is known, and it is still
        replaced.
        """
        try:
            disabled, _ = self._load()
        except (OSError, ValueError):
            self._save([])
            return 0
        if not disabled:
            return 0
        self._save([])
        return len(disabled)

    def snapshot(self) -> dict[str, object]:
        """What the record holds, or why it could not be read. Never raises."""
        try:
            disabled, updated_at = self._load()
        except (OSError, ValueError) as exc:
            return {
                "schema": self.SCHEMA,
                "disabled": [],
                "updated_at": None,
                "reason": str(exc)[:448],
            }
        return {
            "schema": self.SCHEMA,
            "disabled": list(disabled),
            "updated_at": updated_at,
            "reason": None,
        }

    def _load(self) -> tuple[list[str], int | None]:
        raw = load_json(self.path, {"schema": self.SCHEMA, "disabled": [], "updated_at": None})
        schema = raw.get("schema") if isinstance(raw, dict) else None
        if (
            not isinstance(raw, dict)
            or set(raw) - {"schema", "disabled", "updated_at"}
            or isinstance(schema, bool)
            or not isinstance(schema, int)
            or schema != self.SCHEMA
            or not isinstance(raw.get("disabled"), list)
        ):
            raise ValueError("provider source selection has an unsupported or corrupt schema")
        entries = raw["disabled"]
        if len(entries) > MAX_DISABLED_PROVIDERS:
            raise ValueError("provider source selection records too many switched-off providers")
        disabled: list[str] = []
        for item in entries:
            provider = normalized_provider_id(item)
            if provider in disabled:
                raise ValueError("provider source selection records a provider twice")
            disabled.append(provider)
        return disabled, _timestamp(raw.get("updated_at"))

    def _save(self, disabled: list[str]) -> None:
        payload = {
            "schema": self.SCHEMA,
            # Sorted so the same set of refusals is the same file, whatever
            # order the presses arrived in.
            "disabled": sorted(disabled),
            "updated_at": int(time.time()),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # No directory sync of its own. `atomic_write_bytes` already syncs the
        # parent after `os.replace` and raises `DurabilityUnknownError` when
        # that fails, which is the one signal that says the content is in place
        # even though the call failed. A second fsync here could only fail
        # after the shared boundary had already passed, as a plain `OSError`
        # that every caller reads as "the backend refused and nothing changed".
        atomic_write_json(self.path, payload)


def _timestamp(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 0xFFFFFFFFFF:
        raise ValueError("provider source selection timestamp is invalid")
    return value
