"""A durable copy of what the panel did, written while it is still working.

The panel already keeps a bounded record of its own behaviour in
`src/supportLog.ts`, and until now that record reached a support bundle only by
being handed across the RPC at the moment the user asked for one.  That works
for every failure the user can still describe, and for exactly one class it
cannot work at all: the panel itself stops.

A wedged Quick Access panel is recovered by restarting Steam's webhelper, which
destroys the renderer the ring buffer lives in.  The evidence of what the panel
was doing is therefore deleted by the only action that makes the device usable
again, and the bundle collected afterwards carries `frontend_entries=0`.  That
is not a hypothetical: the 2026-09-12 panel-close incident was recovered before
anything had read the ring, and nothing about what the panel had been doing
survived it.

So the panel flushes here as it goes.  This file is append-only, bounded, and
outlives the renderer, the plugin reload and the reboot.  It answers one
question the live ring cannot: what was the last thing the panel did before it
stopped doing anything, and did it stop because it was closed or because it
wedged.  A clean close ends with `panel.dismounted`; a wedge ends mid-work.

Everything written here has already passed `normalize_frontend_log`, which is
the same redaction the live ring's entries pass on their way into the archive.
This is deliberately the identical function and not a second implementation of
the same rule.
"""
from __future__ import annotations

import errno
import json
import os
import threading
import time
from pathlib import Path

from .activity_log import safe_log_text
from .atomic import atomic_write_bytes


# The journal is trimmed to this once it grows past the trigger below. A wedge
# is diagnosed from the end of the record, so the tail is what is kept.
MAX_JOURNAL_BYTES = 512 * 1024
# Trimming rewrites the file, so it is not done on every append. Growing this
# much past the retained size is what pays for one rewrite.
TRIM_TRIGGER_BYTES = MAX_JOURNAL_BYTES + 128 * 1024
# Entries accepted from one flush. The panel's own ring holds 500, so a flush
# larger than this is not a panel that has been busy.
MAX_FLUSH_ENTRIES = 600
# Private to the user this plugin runs as. The journal holds what the panel did,
# which is game names, table identities and refusals, and it sits in the plugin
# state directory for as long as the device keeps it. Every route that creates
# or replaces the file states this rather than taking whatever the umask gives.
JOURNAL_MODE = 0o600


# Serialises append-then-trim against itself.
#
# The trim reads the tail, writes a replacement and renames it over the file, so
# an append landing inside that window would be dropped by the rename with no
# sign that it ever happened, and two trims at once would each rewrite the file
# from a tail read before the other.  The panel flushes one at a time, so this
# is never contended in practice;
# it is here because the cost of being wrong is evidence disappearing silently,
# which is the failure this whole file exists to prevent. It guards this
# process only, and one plugin process is the only writer.
_WRITE_LOCK = threading.Lock()


def journal_path(state_root: Path) -> Path:
    """Where the panel's flushed record lives."""
    return state_root / "frontend-journal.jsonl"


def append_entries(
    path: Path,
    entries: list[dict[str, object]],
    *,
    dropped: int = 0,
    session: str = "",
) -> int:
    """Append already-normalized panel entries, and return how many landed.

    Appending is `O_APPEND` writes of complete lines, so a flush that races
    another writer interleaves whole records rather than corrupting one, and a
    reader that arrives mid-write sees a short final line it can drop.

    A failure here is never allowed to reach the panel: this is diagnostics, and
    a full or read-only filesystem must not turn into a press that does nothing.
    The caller logs the refusal; the panel is told how many entries were taken.
    That answer is what the panel retires its own copy on, so it is only given
    for bytes that actually reached the file.
    """
    if not entries:
        return 0
    written = 0
    lines: list[str] = []
    for entry in entries[:MAX_FLUSH_ENTRIES]:
        record = dict(entry)
        if session:
            record["session"] = safe_log_text(session, 40)
        if dropped:
            record["dropped_before"] = int(dropped)
        try:
            lines.append(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        except (TypeError, ValueError):
            continue
        written += 1
    if not lines:
        return 0
    payload = ("\n".join(lines) + "\n").encode("utf-8", "replace")
    path.parent.mkdir(parents=True, exist_ok=True)
    with _WRITE_LOCK:
        # O_APPEND rather than "a" plus a seek: the atomicity this relies on is
        # the kernel's, and it only holds for a descriptor opened with the flag.
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, JOURNAL_MODE)
        try:
            _append_all(handle, payload)
        finally:
            os.close(handle)
        _trim(path)
    return written


def _append_all(handle: int, payload: bytes) -> None:
    """Write every byte of one flush, or raise saying it did not.

    `os.write` is allowed to write fewer bytes than it was given and return that
    count without raising, which a full device, a signal and a slow target can
    each produce. Taking one call for the whole write answered the panel with a
    success for a batch whose tail never reached the file, and the panel retires
    an entry from its hand-over queue on that answer: the evidence would then be
    gone from the live ring and from the file at the same moment, which is
    exactly when storage is under pressure and a report is most wanted.

    A write that makes no progress at all is a failure rather than something to
    spin on.
    """
    view = memoryview(payload)
    written = 0
    try:
        while view:
            count = os.write(handle, view)
            if count <= 0:
                raise OSError(errno.ENOSPC, "the journal accepted none of the bytes offered to it")
            written += count
            view = view[count:]
    except OSError:
        # Whatever landed is half a record. End the line so a reader drops that
        # one, rather than letting the next append join onto it and cost both.
        if written and payload[written - 1] != 0x0A:
            try:
                os.write(handle, b"\n")
            except OSError:
                pass
        raise


def _trim(path: Path) -> None:
    """Keep the newest `MAX_JOURNAL_BYTES`, on a whole-line boundary.

    Rewrite-and-replace rather than truncate-in-place, so a reader never sees a
    file whose front has been cut away under it. Called with `_WRITE_LOCK` held,
    because the window between reading the tail here and renaming over the file
    is one an append must not land in.

    Through the project's own atomic write, which sets the mode explicitly. A
    replacement written with `Path.write_bytes` takes the process umask, so on
    an ordinary `022` device the first trim turned a file deliberately created
    `0600` into `0644` and left it that way: the appends after it reopen the
    file rather than recreating it, so nothing restored the mode afterwards.
    This file is what the panel did, for as long as the device keeps it.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size <= TRIM_TRIGGER_BYTES:
        return
    try:
        with path.open("rb") as handle:
            handle.seek(size - MAX_JOURNAL_BYTES)
            tail = handle.read()
    except OSError:
        return
    # The seek almost certainly landed inside a record; drop that fragment.
    newline = tail.find(b"\n")
    tail = tail[newline + 1:] if newline >= 0 else b""
    try:
        atomic_write_bytes(path, tail, mode=JOURNAL_MODE)
    except OSError:
        # Diagnostics: a journal that could not be trimmed is a file that stays
        # large, never a flush that failed. The next append tries again.
        return


def journal_position(path: Path) -> dict[str, object]:
    """Where the record stands now, as a boundary a later read can start from.

    A boundary rather than a moment, because the moment is the panel's own wall
    clock and this file outlives the renderer that wrote it: a clock corrected
    backwards leaves records stamped later than the clock a reader is holding,
    and a reader filtering on that would take the previous frontend's entries as
    this one's. Where the file ends is not a claim about time and cannot be
    moved by one.

    The identity of the file goes with the size, because the trim above replaces
    it rather than truncating it, and an offset into a file that has since been
    replaced points at somebody else's bytes.
    """
    try:
        info = path.stat()
    except OSError:
        # No record yet is a boundary too, and the honest one: everything that
        # appears afterwards was appended after this.
        return {"present": False, "size": 0, "inode": 0, "device": 0}
    return {"present": True, "size": info.st_size, "inode": info.st_ino, "device": info.st_dev}


def read_appended(path: Path, boundary: dict[str, object], *, max_bytes: int) -> dict[str, object]:
    """What was appended after a boundary, and whether that boundary still holds.

    `valid` is false when the file this boundary was taken from is not the file
    being read now: it was replaced by a trim, or truncated, or has gone. There
    is nothing to salvage in that case and saying so is the answer, because the
    bytes that are there cannot be placed relative to the boundary any more.

    `complete` is whether everything appended since fitted in `max_bytes`. A
    reader pairing records needs both halves of a pair, so a read that had to
    stop early is not a smaller answer, it is a different one.
    """
    empty = {"data": b"", "valid": False, "complete": False}
    size = int(boundary.get("size", 0) or 0)
    try:
        handle = path.open("rb")
    except OSError:
        # No file and a boundary that said there was none is not a lost
        # boundary: it is a record nothing has been appended to yet, which is
        # the ordinary state of a device whose panel has not flushed since. A
        # file that was there and is not any more is the other thing.
        return {"data": b"", "valid": not boundary.get("present"), "complete": True}
    try:
        with handle:
            # The open file rather than the name, and that is the point. The
            # writer is another process, so nothing here excludes its trim,
            # which keeps the tail and renames a replacement over this path. A
            # name checked and then opened is a check of one file and a read of
            # another, and a replacement large enough to reach the old offset
            # would have its retained tail read as newly appended records. What
            # is validated is therefore the descriptor the bytes come from.
            info = os.fstat(handle.fileno())
            if boundary.get("present"):
                if (info.st_ino, info.st_dev) != (boundary.get("inode"), boundary.get("device")):
                    return empty
                if info.st_size < size:
                    return empty
            elif size:
                return empty
            # A boundary is taken between appends, which are whole lines, so it
            # is a line start. Proven rather than assumed: a byte before it that
            # is not a newline means the boundary landed inside a record, and
            # that record belongs to what came before it.
            partial = False
            if size:
                handle.seek(size - 1)
                partial = handle.read(1) != b"\n"
            else:
                handle.seek(0)
            data = handle.read(max_bytes + 1)
    except OSError:
        return empty
    complete = len(data) <= max_bytes
    data = data[:max_bytes]
    if partial:
        newline = data.find(b"\n")
        data = data[newline + 1:] if newline >= 0 else b""
    return {"data": data, "valid": True, "complete": complete}


def read_tail(path: Path, *, max_bytes: int) -> tuple[bytes, bool]:
    """The end of the journal, and whether anything was cut from its front."""
    try:
        size = path.stat().st_size
    except OSError:
        return b"", False
    try:
        with path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
                data = handle.read()
                newline = data.find(b"\n")
                return (data[newline + 1:] if newline >= 0 else b""), True
            return handle.read(), False
    except OSError:
        return b"", False


def summarize(path: Path) -> dict[str, object]:
    """What the journal holds, for the bundle manifest and for a probe.

    The last recorded event is the whole point of this file, so it is reported
    as its own field rather than left to be read out of the archive: a panel
    that closed cleanly ends on `panel.dismounted`, and one that was killed
    while wedged ends on whatever it was doing.
    """
    try:
        stat = path.stat()
    except OSError:
        # The same keys as the answer below, so a caller that indexes this does
        # not have to know which branch produced it.
        return {
            "present": False, "entries": 0, "bytes": 0, "front_truncated": False,
            "modified": None, "last_event": None, "last_at": None, "last_level": None,
        }
    data, truncated = read_tail(path, max_bytes=MAX_JOURNAL_BYTES)
    entries = 0
    last: dict[str, object] | None = None
    for line in data.splitlines():
        if not line.strip():
            continue
        entries += 1
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            last = parsed
    return {
        "present": True,
        "entries": entries,
        "bytes": stat.st_size,
        "front_truncated": truncated,
        "modified": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(stat.st_mtime)),
        "last_event": (last or {}).get("event"),
        "last_at": (last or {}).get("at"),
        "last_level": (last or {}).get("level"),
    }
