"""What may not start in a game until the run of it that is going now is over.

Two holds, one record per game, both about one run of that game:

- `dirty`: a stop ended Cheat Engine without proving every cheat put down or
  the game gone. What that Cheat Engine left changed can no longer be put back,
  because its restore resolves symbols belonging to the process that was ended,
  and a table started on top of it meets patches it did not write: its scans
  miss, its startup fails and the failure is recorded against a table that
  works. Every start in that game is refused while it stands.
- `stopped`: the user stopped Cheat Engine in that game. Auto-load starts the
  table whenever the game runs without it, which is exactly what a Stop leaves,
  so only Auto-load is held.

Owned by the backend and kept on disk, because the panel is remounted, the
plugin is reloaded and more than one game can be running, and none of those may
lift a hold on a game that is still running what the hold was about.

A hold ends only on proof that its run is over: every process of that run gone,
or where those could not be listed, every program the session was pointed at
proven absent by a complete walk. Not knowing keeps it. The user may still
clear one, after being told what it is, because a device whose process table
never answers would otherwise hold that game for ever.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time

from .atomic import atomic_write_json, load_json
from .ce_launch import TargetIdentity

SCHEMA = 1
KINDS = ("dirty", "stopped")
# Games held at once. Far beyond anyone's running set; past it the oldest
# Auto-load hold goes first, and a dirty one only once none of those is left.
MAX_HELD_GAMES = 64
MAX_TARGETS = 16
MAX_IDENTITIES = 64
MAX_BYTES = 256 * 1024


@dataclass(frozen=True)
class RunHold:
    app_id: int
    kind: str
    since: float
    # The programs the session was pointed at; `None` where its record could
    # not be read, which leaves nothing to prove absent by name.
    targets: tuple[str, ...] | None
    # The processes of the run, where they could all be listed.
    identities: tuple[TargetIdentity, ...] | None
    unsettled: int = 0

    def public(self) -> dict[str, object]:
        return {"since": self.since, "unsettled": self.unsettled}


class GameRunHolds:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[int, dict[str, RunHold]]:
        """Every hold on disk, or none where the file cannot be read.

        An unreadable file is read as holding nothing and the caller is told,
        because the other reading refuses every start in every game with nothing
        the user can do about it.
        """
        raw = load_json(self.path, None, max_bytes=MAX_BYTES)
        if raw is None:
            return {}
        if not isinstance(raw, dict) or raw.get("schema") != SCHEMA or not isinstance(raw.get("apps"), dict):
            raise ValueError("game run holds are unreadable")
        found: dict[int, dict[str, RunHold]] = {}
        for key, entry in list(raw["apps"].items())[:MAX_HELD_GAMES * 2]:
            app_id = int(key)
            if app_id <= 0 or not isinstance(entry, dict):
                raise ValueError("game run holds are unreadable")
            holds = {kind: _parse(app_id, kind, entry[kind]) for kind in KINDS if isinstance(entry.get(kind), dict)}
            if holds:
                found[app_id] = holds
        return found

    def save(self, holds: dict[int, dict[str, RunHold]]) -> None:
        apps = {
            str(app_id): {kind: _render(hold) for kind, hold in kinds.items()}
            for app_id, kinds in _bounded(holds).items() if kinds
        }
        atomic_write_json(self.path, {"schema": SCHEMA, "apps": apps}, max_bytes=MAX_BYTES)


def new_hold(
    app_id: int, kind: str, targets: tuple[str, ...] | None,
    identities: tuple[TargetIdentity, ...] | None, unsettled: int = 0,
) -> RunHold:
    if kind not in KINDS:
        raise ValueError("unknown game run hold")
    return RunHold(
        app_id=app_id, kind=kind, since=time.time(),
        targets=None if targets is None else tuple(targets[:MAX_TARGETS]),
        # A list cut short is not the run's list, and proving a shorter one gone
        # would lift the hold while processes past the cut were still running.
        identities=None if identities is None or len(identities) > MAX_IDENTITIES else tuple(identities),
        unsettled=max(0, int(unsettled)),
    )


def _bounded(holds: dict[int, dict[str, RunHold]]) -> dict[int, dict[str, RunHold]]:
    kept = {app_id: dict(kinds) for app_id, kinds in holds.items() if kinds}
    for kind in ("stopped", "dirty"):
        while len(kept) > MAX_HELD_GAMES:
            candidates = [(hold.since, app_id) for app_id, kinds in kept.items() for k, hold in kinds.items() if k == kind]
            if not candidates:
                break
            _since, app_id = min(candidates)
            kept[app_id].pop(kind)
            if not kept[app_id]:
                kept.pop(app_id)
    return kept


def _render(hold: RunHold) -> dict[str, object]:
    return {
        "since": hold.since,
        "targets": None if hold.targets is None else list(hold.targets),
        "identities": None if hold.identities is None else [
            [identity.pid, identity.start_time, identity.windows_executable] for identity in hold.identities
        ],
        "unsettled": hold.unsettled,
    }


def _parse(app_id: int, kind: str, raw: dict[str, object]) -> RunHold:
    since = raw.get("since")
    targets = raw.get("targets")
    identities = raw.get("identities")
    unsettled = raw.get("unsettled", 0)
    if (
        isinstance(since, bool) or not isinstance(since, (int, float))
        or (targets is not None and (not isinstance(targets, list) or not all(isinstance(t, str) for t in targets)))
        or (identities is not None and not isinstance(identities, list))
        or isinstance(unsettled, bool) or not isinstance(unsettled, int)
    ):
        raise ValueError("game run holds are unreadable")
    parsed: tuple[TargetIdentity, ...] | None = None
    if identities is not None:
        items: list[TargetIdentity] = []
        for item in identities[:MAX_IDENTITIES + 1]:
            if (
                not isinstance(item, list) or len(item) != 3
                or not all(isinstance(value, int) and not isinstance(value, bool) for value in item[:2])
                or not isinstance(item[2], str)
            ):
                raise ValueError("game run holds are unreadable")
            items.append(TargetIdentity(pid=item[0], start_time=item[1], windows_executable=item[2], app_id=app_id))
        parsed = None if len(items) > MAX_IDENTITIES else tuple(items)
    return RunHold(
        app_id=app_id, kind=kind, since=float(since),
        targets=None if targets is None else tuple(targets[:MAX_TARGETS]),
        identities=parsed, unsettled=max(0, unsettled),
    )
