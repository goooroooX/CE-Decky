from __future__ import annotations

from pathlib import Path
import json
import os
import tempfile
import stat
from typing import Any

MAX_JSON_BYTES = 4 * 1024 * 1024

# Every reader below checks that it has a regular file, and every one of them
# checked it after the open. Opening a FIFO for reading blocks until somebody
# opens the other end, so a path that is not a regular file could hang the call
# rather than be refused by it: measured here, a ranged read of a FIFO never
# returned. `O_NONBLOCK` makes the open itself return, and the check that was
# always there then refuses it. It changes nothing for a regular file, and it is
# absent on Windows, where the whole case is.
_NONBLOCKING_OPEN = getattr(os, "O_NONBLOCK", 0)


class DurabilityUnknownError(OSError):
    """The new content is already in place and its durability is not established.

    ``os.replace`` publishes the replacement before the directory entry is
    synced, so a failure after it is a different fact from a refusal before it:
    the new authority is visible to every reader right now, including a resident
    Cheat Engine reading its control file, and only its survival of a power loss
    is in doubt. Callers used to see one Python exception for both, and the
    frontend reads a Python traceback as proof the backend refused - so a write
    that had landed was reported as definitely rejected, and the retry offered
    for it acted on state that had already changed. The class name is what
    crosses the RPC boundary, so it is part of the contract.
    """


def durable_rename(source: Path, destination: Path) -> None:
    """Rename authority; a sync failure after rename is never a refusal."""
    os.rename(source, destination)
    try:
        fsync_directory(destination.parent)
        if source.parent != destination.parent:
            fsync_directory(source.parent)
    except OSError as exc:
        raise DurabilityUnknownError(
            f"{source.name} was renamed; directory durability is unknown: {exc}"
        ) from exc


def durable_unlink(path: Path, *, missing_ok: bool = False) -> None:
    """Remove an entry and commit its absence, including on a retry."""
    path.unlink(missing_ok=missing_ok)
    try:
        parent = path.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        fsync_directory(parent)
    except OSError as exc:
        raise DurabilityUnknownError(
            f"{path.name} is absent; directory durability is unknown: {exc}"
        ) from exc


def atomic_write_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Replace a file's contents in one step.

    Raises ``DurabilityUnknownError`` for a failure after the replacement is
    visible, and an ordinary exception for every failure before it, which is the
    only distinction that tells a caller whether anything happened.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
        try:
            fsync_directory(path.parent)
        except OSError as exc:
            raise DurabilityUnknownError(
                f"{path.name} was replaced and the directory could not be synced ({exc}); "
                "the new content is in place and may not survive a power loss"
            ) from exc
    finally:
        temp_path.unlink(missing_ok=True)


def atomic_write_json(path: Path, value: Any, *, max_bytes: int = MAX_JSON_BYTES) -> None:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("JSON state max_bytes must be a positive integer")
    try:
        data = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("JSON state contains text that is not valid Unicode") from exc
    except ValueError as exc:
        raise ValueError(f"JSON state contains a non-serializable numeric value: {exc}") from exc
    if len(data) > max_bytes:
        raise ValueError("JSON state payload exceeds size limit")
    atomic_write_bytes(path, data)


def load_json(path: Path, default: Any, *, max_bytes: int = MAX_JSON_BYTES) -> Any:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("JSON state max_bytes must be a positive integer")
    # Check symlink before exists(): a dangling link must not silently look like
    # absent state. O_NOFOLLOW then closes the check/open replacement race on
    # platforms that provide it. The descriptor read itself is bounded too, so a
    # file that grows after fstat cannot turn a small-state read into an unbounded
    # allocation.
    if path.is_symlink():
        raise ValueError("JSON state path must be a regular file")
    flags = os.O_RDONLY | _NONBLOCKING_OPEN | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return default
    except OSError as exc:
        raise ValueError("JSON state path could not be opened safely") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("JSON state path must be a regular file")
        if info.st_size <= 0 or info.st_size > max_bytes:
            raise ValueError("JSON state file size is invalid")
        data = bytearray()
        while len(data) <= max_bytes:
            chunk = os.read(fd, min(1024 * 1024, max_bytes + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if not data or len(data) > max_bytes:
            raise ValueError("JSON state file size is invalid")
        final_info = os.fstat(fd)
        _ensure_stable_descriptor_read(info, final_info, len(data), "JSON state")
        try:
            text = bytes(data).decode("utf-8")
            return json.loads(
                text,
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
        except (json.JSONDecodeError, UnicodeError, ValueError) as exc:
            if isinstance(exc, ValueError) and str(exc).startswith("JSON state"):
                raise
            raise ValueError("JSON state file is not valid strict UTF-8 JSON") from exc
    finally:
        os.close(fd)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON state contains duplicate object key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"JSON state contains non-finite number: {value}")



def read_regular_bytes(path: Path, *, max_bytes: int, allow_missing: bool = False, allow_empty: bool = False) -> bytes | None:
    """Read a bounded regular file without following the final symlink component."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    if path.is_symlink():
        raise ValueError("file path must be a regular file")
    flags = os.O_RDONLY | _NONBLOCKING_OPEN | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        if allow_missing:
            return None
        raise ValueError("file is missing")
    except OSError as exc:
        raise ValueError("file could not be opened safely") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("file path must be a regular file")
        if info.st_size > max_bytes or (info.st_size <= 0 and not allow_empty):
            raise ValueError("file size is invalid")
        data = bytearray()
        while len(data) <= max_bytes:
            chunk = os.read(fd, min(1024 * 1024, max_bytes + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > max_bytes:
            raise ValueError("file size exceeds limit while reading")
        if not data and not allow_empty:
            raise ValueError("file size is invalid")
        final_info = os.fstat(fd)
        _ensure_stable_descriptor_read(info, final_info, len(data), "file")
        return bytes(data)
    finally:
        os.close(fd)


def read_proc_bytes(path: Path, *, max_bytes: int) -> bytes | None:
    """Read one bounded procfs record without trusting its synthetic size.

    Linux reports ``st_size == 0`` for records such as ``stat`` and
    ``environ`` even when reading their descriptor yields data. The ordinary
    managed-file reader correctly requires bytes read to equal ``st_size``, so
    procfs needs this narrower descriptor-identity check instead.
    """
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("procfs max_bytes must be a positive integer")
    if path.is_symlink():
        raise ValueError("procfs path must be a regular file")
    flags = os.O_RDONLY | _NONBLOCKING_OPEN | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError("procfs record could not be opened safely") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("procfs path must be a regular file")
        data = bytearray()
        while len(data) <= max_bytes:
            chunk = os.read(fd, min(4096, max_bytes + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > max_bytes:
            raise ValueError("procfs record exceeds configured bound")
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise ValueError("procfs record changed while reading")
        return bytes(data)
    finally:
        os.close(fd)


def read_regular_bytes_with_stat(
    path: Path,
    *,
    max_bytes: int,
    allow_missing: bool = False,
    allow_empty: bool = False,
) -> tuple[bytes | None, os.stat_result | None]:
    """Read bounded bytes and return metadata from that exact open descriptor."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    if path.is_symlink():
        raise ValueError("file path must be a regular file")
    flags = os.O_RDONLY | _NONBLOCKING_OPEN | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        if allow_missing:
            return None, None
        raise ValueError("file is missing")
    except OSError as exc:
        raise ValueError("file could not be opened safely") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("file path must be a regular file")
        if info.st_size > max_bytes or (info.st_size <= 0 and not allow_empty):
            raise ValueError("file size is invalid")
        data = bytearray()
        while len(data) <= max_bytes:
            chunk = os.read(fd, min(1024 * 1024, max_bytes + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > max_bytes:
            raise ValueError("file size exceeds limit while reading")
        if not data and not allow_empty:
            raise ValueError("file size is invalid")
        final_info = os.fstat(fd)
        _ensure_stable_descriptor_read(info, final_info, len(data), "file")
        return bytes(data), final_info
    finally:
        os.close(fd)


def read_regular_range(path: Path, *, offset: int, length: int) -> tuple[bytes, os.stat_result]:
    """Read one span of a regular file, with the identity that span came from.

    The whole-file readers above are the wrong tool for a large artifact whose
    interesting part is a known span: a version resource sits at a known offset
    inside an executable that may be hundreds of megabytes, and reading the file
    to reach it makes the file's size the bound rather than the structure's. The
    guarantees are the ones those readers give - no final symlink component, a
    regular file, one descriptor, and a refusal when that inode changes
    underneath the read.

    The span is clipped at end of file rather than padded, so a result shorter
    than ``length`` means the file ends there. The stat this exact descriptor
    reported is returned with the bytes, because a caller reading several spans
    has to prove they came from one unchanged file.
    """
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    if isinstance(length, bool) or not isinstance(length, int) or length <= 0:
        raise ValueError("length must be a positive integer")
    if path.is_symlink():
        raise ValueError("file path must be a regular file")
    flags = os.O_RDONLY | _NONBLOCKING_OPEN | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise ValueError("file is missing")
    except OSError as exc:
        raise ValueError("file could not be opened safely") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("file path must be a regular file")
        expected = max(0, min(length, info.st_size - offset))
        data = bytearray()
        if expected:
            os.lseek(fd, offset, os.SEEK_SET)
            while len(data) < expected:
                chunk = os.read(fd, min(1024 * 1024, expected - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
        final_info = os.fstat(fd)
        _ensure_stable_range_read(info, final_info, len(data), expected)
        return bytes(data), final_info
    finally:
        os.close(fd)


def _ensure_stable_range_read(before: os.stat_result, after: os.stat_result, bytes_read: int, expected: int) -> None:
    """Reject a span read from an inode that changed while it was being read.

    The whole-file guard compares bytes read against the file size, which a span
    is not. What is compared here is the span actually asked of this file: a
    short read of a file whose size never moved is a truncation between the two
    stats or a descriptor that stopped answering, and neither may pass as data.
    """
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    if identity_before != identity_after or bytes_read != expected:
        raise ValueError("file changed while reading")


def _ensure_stable_descriptor_read(before: os.stat_result, after: os.stat_result, bytes_read: int, field: str) -> None:
    """Reject in-place mutation while an already-open managed file is being read.

    Atomic path replacement is safe because the open descriptor keeps referencing the
    original inode. This guard targets truncation/overwrite of that same inode.
    """
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    if identity_before != identity_after or bytes_read != after.st_size:
        raise ValueError(f"{field} changed while reading")


def fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(path, flags)
    except OSError:
        # POSIX target durability requires the directory entry created by
        # os.replace() to be fsynced. A successful RPC must not silently claim a
        # durable state transition when that directory durability boundary could
        # not even be opened. Windows development environments do not provide a
        # portable directory-fsync contract, so retain the previous best-effort
        # behavior there; SteamOS/Linux is the product authority for this claim.
        if os.name == "posix":
            raise
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# Backward-compatible internal alias for older tests/callers.
_fsync_dir = fsync_directory
