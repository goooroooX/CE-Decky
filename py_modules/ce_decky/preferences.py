"""The user's own on/off choices, kept where an older build cannot trip over them.

These started as two more fields in `config.json`, and that was wrong in a way
only a device could show. That file carries identity - which Cheat Engine is
registered, its digest, its root - and it is parsed strictly, so a build that
does not know a field refuses the whole file. Adding a field to it therefore
made every earlier build read a device with a registered Cheat Engine as a
device with none: observed on a Steam Deck updating from this version to the
published one, where the panel said Cheat Engine was not installed while the
installation and its registration sat untouched on disk.

So preferences live on their own, and this file is the opposite kind of file.
Nothing here authorizes anything, so an unreadable one is not a refusal: it
reads as the defaults, which is what a device with no preferences file has
anyway. A key it does not know is ignored rather than fatal, which is the half
that was missing: the next version to add a preference must not do to this one
what this one did to the version before it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .atomic import atomic_write_json, load_json

SCHEMA = 1
MAX_PREFERENCES_BYTES = 16 * 1024
#: The names this used to be stored under, inside `config.json`.
LEGACY_CONFIG_KEYS = ("update_auto_check", "mascot_visible")


@dataclass(frozen=True)
class Preferences:
    """Both default to on, which is what every device had before they existed."""

    update_auto_check: bool = True
    mascot_visible: bool = True

    def public(self) -> dict[str, object]:
        return asdict(self)


def _flag(raw: dict[str, Any], field: str, default: bool = True) -> bool:
    value = raw.get(field, default)
    # A value that is not a boolean is a file somebody edited or a write that
    # was interrupted. Neither is a reason to refuse the panel a preference it
    # can perfectly well default.
    return value if isinstance(value, bool) else default


class PreferenceStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> Preferences:
        """The stored choices. Never raises: absent and unreadable are defaults."""
        try:
            raw = load_json(self.path, {}, max_bytes=MAX_PREFERENCES_BYTES)
        except (ValueError, OSError):
            return Preferences()
        if not isinstance(raw, dict):
            return Preferences()
        return Preferences(
            update_auto_check=_flag(raw, "update_auto_check"),
            mascot_visible=_flag(raw, "mascot_visible"),
        )

    def save(self, preferences: Preferences) -> Preferences:
        atomic_write_json(
            self.path, {"schema": SCHEMA, **asdict(preferences)}, max_bytes=MAX_PREFERENCES_BYTES,
        )
        return preferences

    def set(self, **fields: bool) -> Preferences:
        """Change one choice and keep the rest. Returns what is now stored."""
        unknown = set(fields) - set(asdict(Preferences()))
        if unknown:
            raise ValueError(f"unknown preference: {sorted(unknown)[0]}")
        for value in fields.values():
            if not isinstance(value, bool):
                raise ValueError("a preference is on or off")
        return self.save(Preferences(**{**asdict(self.load()), **fields}))
