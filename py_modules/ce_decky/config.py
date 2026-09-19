from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
import re
import unicodedata
from typing import Any

from .atomic import atomic_write_json, load_json
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
    "update_auto_check",
    "mascot_visible",
}


@dataclass
class Config:
    schema: int = 1
    imported_ce_executable: str | None = None
    imported_ce_sha256: str | None = None
    imported_ce_root: str | None = None
    imported_ce_version: str | None = None
    acknowledged_security_notice: bool = False
    # Both default to on, and both are absent from every file written before
    # 0.9.28. An absent key is therefore the default rather than an error, which
    # is what lets a device that has been running this plugin for months read
    # its own configuration after an update; `ConfigStore.migrate` is what then
    # writes the defaults down so the file says what this version would write.
    update_auto_check: bool = True
    mascot_visible: bool = True

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "Config":
        if not isinstance(raw, dict) or set(raw) - _ALLOWED_KEYS:
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
        auto_check = _preference(raw, "update_auto_check")
        mascot_visible = _preference(raw, "mascot_visible")
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
            update_auto_check=auto_check,
            mascot_visible=mascot_visible,
        )


def _preference(raw: dict[str, Any], field: str) -> bool:
    """One on/off preference, defaulting to on where the file predates it.

    Absent is deliberately not an error: every configuration written before
    these preferences existed lacks them, and refusing such a file would leave
    an updated device unable to read its own registered Cheat Engine. A value
    that is present must still be a boolean, because a file that carries this
    key carries a decision the user made.
    """
    value = raw.get(field, True)
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be boolean")
    return value


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

    def migrate(self) -> bool:
        """Write the file this version would write, once. Returns whether it did.

        A configuration written by an earlier build carries only the keys that
        build had, and every key this one added reads as its default. That is
        enough to run on, and it is not enough to leave: the file on a device
        that has been updated should say what this version holds, so that a
        preference the user never touched is visible where they would look for
        it and so the next build that adds a key finds one shape rather than a
        history of them.

        Every existing value is preserved exactly, because the rewrite is the
        validated load of what is already there. An absent file is the same
        case with nothing to preserve, which is what the first run writes.

        The caller is an explicit mutation boundary at load. It is deliberately
        not a read path: read-only helpers call `get_status()` and promise to
        write nothing.
        """
        raw = load_json(self.path, {}, max_bytes=256 * 1024)
        if not isinstance(raw, dict):
            raise ValueError("config root must be a JSON object")
        config = Config.from_mapping(raw)
        if config.schema != 1:
            raise ValueError(f"unsupported config schema: {config.schema}")
        if _ALLOWED_KEYS <= set(raw):
            return False
        self.save(config)
        return True

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
