from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
import os
import re
import stat
import struct
import unicodedata


_WINDOWS_CE_NAMES = (
    "cheatengine-x86_64.exe",
    "cheatengine-x86_64-SSE4-AVX2.exe",
    "Cheat Engine.exe",
    "cheatengine-i386.exe",
)
_CE_NAME_PATTERN = re.compile(r"cheat[ _-]*engine|cheatengine", re.I)


@dataclass(frozen=True)
class ImportedCE:
    executable: str
    root: str
    sha256: str
    size: int

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def inspect_ce_selection(selection: str) -> ImportedCE:
    path = Path(selection).expanduser().resolve()
    executable = _resolve_ce_executable(path)
    if executable.suffix.lower() != ".exe":
        raise ValueError("stable v1 requires the Windows Cheat Engine executable")
    if not _CE_NAME_PATTERN.search(executable.name):
        raise ValueError("selected executable name is not recognizable as Cheat Engine")
    size, digest = _inspect_exact_executable(executable)
    return ImportedCE(
        executable=str(executable),
        root=str(executable.parent),
        sha256=digest,
        size=size,
    )


def _resolve_ce_executable(path: Path) -> Path:
    if path.is_file():
        return path
    if not path.is_dir():
        raise ValueError("selected Cheat Engine path is neither a file nor directory")
    files = [p for p in path.iterdir() if p.is_file()]
    by_windows_name: dict[str, list[Path]] = {}
    for candidate in files:
        key = unicodedata.normalize("NFC", candidate.name).rstrip(" .").casefold()
        by_windows_name.setdefault(key, []).append(candidate)
    ambiguous = [items for items in by_windows_name.values() if len(items) > 1]
    if ambiguous:
        names = ", ".join(sorted(item.name for items in ambiguous for item in items))
        raise ValueError(f"selected Cheat Engine directory contains Windows-ambiguous filenames: {names}")
    exact = {unicodedata.normalize("NFC", p.name).rstrip(" .").casefold(): p for p in files}
    for name in _WINDOWS_CE_NAMES:
        hit = exact.get(unicodedata.normalize("NFC", name).casefold())
        if hit:
            return hit
    candidates = sorted(
        p for p in files
        if p.suffix.lower() == ".exe" and _CE_NAME_PATTERN.search(p.name)
    )
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise ValueError("no recognizable Windows Cheat Engine executable found in selected directory")
    raise ValueError("multiple possible Cheat Engine executables found; select the executable file explicitly")


def _inspect_exact_executable(path: Path) -> tuple[int, str]:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError as exc:
        raise ValueError("Cheat Engine executable does not exist") from exc
    except OSError as exc:
        raise ValueError("Cheat Engine executable could not be opened safely") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Cheat Engine executable must be a regular file")
        if before.st_size < 256 * 1024:
            raise ValueError("selected executable is unexpectedly small")
        _validate_pe_fd(fd, before.st_size)
        digest = _sha256_fd(fd)
        after = os.fstat(fd)
        if _identity(after) != _identity(before):
            raise ValueError("selected executable changed while being inspected; retry import")
        try:
            path_info = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise ValueError("selected executable path changed while being inspected; retry import") from exc
        if not stat.S_ISREG(path_info.st_mode) or _identity(path_info) != _identity(before):
            raise ValueError("selected executable path changed while being inspected; retry import")
        return before.st_size, digest
    finally:
        os.close(fd)


def _validate_pe_fd(fd: int, size: int) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    header = os.read(fd, 64)
    if len(header) < 64 or header[:2] != b"MZ":
        raise ValueError("selected executable is not a valid PE/MZ file")
    pe_offset = struct.unpack_from("<I", header, 0x3C)[0]
    if pe_offset < 64 or pe_offset > min(size - 4, 16 * 1024 * 1024):
        raise ValueError("selected executable has an invalid PE header offset")
    os.lseek(fd, pe_offset, os.SEEK_SET)
    if os.read(fd, 4) != b"PE\x00\x00":
        raise ValueError("selected executable has no PE signature")


def _sha256_fd(fd: int) -> str:
    digest = sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
