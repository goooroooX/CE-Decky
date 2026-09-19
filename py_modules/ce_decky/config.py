from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
import re
import unicodedata
from typing import Any

from .atomic import atomic_write_json, load_json
from .preferences import LEGACY_CONFIG_KEYS
from .text import utf8_len

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_PATH_BYTES = 16 * 1024
_ALLOWED_KEYS = {
    "schema",
    "imported_ce_executable",
    "imported_ce_sha256",
    "imported_ce_root",
    "imported_ce_version",
    "acknowledged_security_notice",
}
# Two keys 0.9.28 briefly stored here before they moved to their own file. They
# are read and dropped rather than refused, because this file is parsed strictly
# and a refusal is the loss of a registered Cheat Engine; `preferences.py`
# carries what that cost on a device. Nothing writes them again.
_MIGRATED_KEYS = frozenset(LEGACY_CONFIG_KEYS)


@dataclass
class Config:
    schema: int = 1
    imported_ce_executable: str | None = None
    imported_ce_sha256: str | None = None
    imported_ce_root: str | None = None
    imported_ce_version: str | None = None
    acknowledged_security_notice: bool = False

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "Config":
        if not isinstance(raw, dict) or set(raw) - _ALLOWED_KEYS - _MIGRATED_KEYS:
            raise ValueError("config contains unknown fields")
        schema = raw.get("schema", 1)
        if isinstance(schema, bool) or not isinstance(schema, int):
            raise ValueError("config schema must be an integer")
        acknowledged = raw.get("acknowledged_security_notice", False)
        if not isinstance(acknowledged, bool):
            raise ValueError("acknowledged_security_notice must be boolean")
        executable = _optional_path(raw.get("imported_ce_executable"), "imported_ce_executable")
        digest = _optional_sha(raw.get("imported_ce_sha256"))
        root = _optional_path(raw.get("imported_ce_root"), "imported_ce_root")
        # The version an executable declares is display metadata, never an
        # identity: it is absent for an executable that declares none, and it
        # can never be the reason a registered Cheat Engine stops validating.
        version = _optional_version(raw.get("imported_ce_version"))
        configured = (executable is not None, digest is not None, root is not None)
        if (any(configured) and not all(configured)) or (version is not None and not all(configured)):
            raise ValueError("imported Cheat Engine identity must contain executable, root, and SHA-256 together")
        return cls(
            schema=schema,
            imported_ce_executable=executable,
            imported_ce_sha256=digest,
            imported_ce_root=root,
            imported_ce_version=version,
            acknowledged_security_notice=acknowledged,
        )


def _optional_version(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("imported_ce_version must be a string or null")
    text = value.strip()
    if (
        not text
        or len(text) > 64
        or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text)
        or any(unicodedata.bidirectional(ch) in {"RLE", "LRE", "RLO", "LRO", "PDF", "RLI", "LRI", "FSI", "PDI"} for ch in text)
    ):
        raise ValueError("imported_ce_version must be short printable text")
    return text


class ConfigStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> Config:
        raw = load_json(self.path, {}, max_bytes=256 * 1024)
        if not isinstance(raw, dict):
            raise ValueError("config root must be a JSON object")
        config = Config.from_mapping(raw)
        if config.schema != 1:
            raise ValueError(f"unsupported config schema: {config.schema}")
        return config

    def take_legacy_preferences(self) -> dict[str, bool]:
        """Lift the preferences 0.9.28 briefly stored here, and rewrite without them.

        They belong in their own file, for the reason `preferences.py` gives:
        this one is parsed strictly, so a key an older build does not know costs
        that build the whole file and with it the registered Cheat Engine. A
        device that ran one of those builds already has them here, and they are
        the user's own choices, so they are moved rather than dropped.

        Returns what was found, so the caller can seed the store that owns them
        now. An empty answer is every ordinary device, and nothing is written.
        """
        raw = load_json(self.path, {}, max_bytes=256 * 1024)
        if not isinstance(raw, dict):
            raise ValueError("config root must be a JSON object")
        found = {
            key: raw[key] for key in LEGACY_CONFIG_KEYS
            if key in raw and isinstance(raw[key], bool)
        }
        if not set(raw) & _MIGRATED_KEYS:
            return found
        self.save(Config.from_mapping(raw))
        return found

    def save(self, config: Config) -> None:
        # Re-validate our own serialized state before persistence so callers cannot
        # bypass invariants by directly constructing a malformed Config instance.
        checked = Config.from_mapping(asdict(config))
        if checked.schema != 1:
            raise ValueError(f"unsupported config schema: {checked.schema}")
        atomic_write_json(self.path, asdict(checked), max_bytes=256 * 1024)


def _optional_path(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string or null")
    text = value.strip()
    if not text:
        return None
    if any(char in text for char in ("\x00", "\r", "\n")) or utf8_len(text, field) > _MAX_PATH_BYTES:
        raise ValueError(f"{field} is invalid or too long")
    return text


def _optional_sha(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("imported_ce_sha256 must be a string or null")
    digest = value.strip().lower()
    if not digest:
        return None
    if not _SHA_RE.fullmatch(digest):
        raise ValueError("imported_ce_sha256 must be a 64-character hexadecimal digest")
    return digest
