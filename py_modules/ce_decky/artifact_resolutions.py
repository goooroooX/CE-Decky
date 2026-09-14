"""Bounded advisory provenance for verified single-table provider artifacts.

This cache never authorizes acquisition or execution and contains no URL.
An unreadable cache supplies no identity; normal extraction remains available.
"""

from pathlib import Path
import re
import threading

from .atomic import atomic_write_json, load_json

MAX_RESOLUTIONS = 4096
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_PROVIDER = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class ArtifactResolutions:
    SCHEMA = 1

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()

    @staticmethod
    def _entry(value: object) -> dict[str, str]:
        fields = {"provider", "artifact_id", "artifact_sha256", "table_sha256"}
        if not isinstance(value, dict) or set(value) != fields or any(not isinstance(v, str) for v in value.values()):
            raise ValueError("invalid artifact resolution")
        if not _PROVIDER.fullmatch(value["provider"]):
            raise ValueError("invalid resolution provider")
        identity = value["artifact_id"]
        if not identity or len(identity.encode("utf-8")) > 512 or any(ord(c) < 32 or ord(c) > 126 for c in identity):
            raise ValueError("invalid resolution artifact identity")
        if not all(_SHA.fullmatch(value[key]) for key in ("artifact_sha256", "table_sha256")):
            raise ValueError("invalid resolution digest")
        return dict(value)

    def _load(self) -> list[dict[str, str]]:
        raw = load_json(self.path, {"schema": self.SCHEMA, "entries": []})
        if not isinstance(raw, dict) or set(raw) != {"schema", "entries"} or type(raw.get("schema")) is not int or raw["schema"] != self.SCHEMA:
            raise ValueError("unsupported artifact resolution schema")
        rows = raw.get("entries")
        if not isinstance(rows, list) or len(rows) > MAX_RESOLUTIONS:
            raise ValueError("invalid artifact resolution count")
        entries = [self._entry(row) for row in rows]
        keys = [(row["provider"], row["artifact_id"], row["artifact_sha256"]) for row in entries]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate artifact resolution identity")
        return entries

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            try:
                return {"schema": self.SCHEMA, "entries": self._load(), "reason": None}
            except (OSError, ValueError) as exc:
                return {"schema": self.SCHEMA, "entries": [], "reason": str(exc)[:256]}

    def record(self, provider: str, artifact_id: str, artifact_sha256: str, table_sha256: str) -> None:
        entry = self._entry(dict(provider=provider, artifact_id=artifact_id,
                                 artifact_sha256=artifact_sha256, table_sha256=table_sha256))
        key = lambda row: (row["provider"], row["artifact_id"], row["artifact_sha256"])
        with self._lock:
            entries = self._load()
            previous = next((row for row in entries if key(row) == key(entry)), None)
            if previous == entry:
                return
            if previous is not None:
                raise ValueError("verified artifact has conflicting table resolutions")
            entries.append(entry)
            atomic_write_json(self.path, {"schema": self.SCHEMA, "entries": entries[-MAX_RESOLUTIONS:]})
