"""Import a Cheat Engine installation directory packed into a .zip archive.

CE Decky's normal path downloads the exact manifest-pinned official installer
and extracts it natively. This module is the fallback for the day that artifact
is no longer downloadable: a user installs Windows Cheat Engine on a Windows
machine, packs the installation directory into a .zip, and imports that file
here.

The archive is untrusted input. Nothing inside it is executed, member paths are
validated before anything is written, the installation root is located rather
than assumed, and the tree is checked for the files a Cheat Engine installation
must have before it is promoted into a plugin-owned directory.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
import os
import stat
import tempfile
import unicodedata
import uuid
import zipfile

from .archive_import import ArchiveImportError, _safe_member
from .atomic import atomic_write_json, fsync_directory, load_json
from .ce_import import ImportedCE, inspect_ce_selection

MAX_CE_ARCHIVE_BYTES = 1024 * 1024 * 1024
MAX_CE_ARCHIVE_MEMBERS = 20_000
MAX_CE_ARCHIVE_TREE_ENTRIES = 40_000
MAX_CE_ARCHIVE_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_CE_ARCHIVE_MEMBER_BYTES = 512 * 1024 * 1024
MAX_CE_ARCHIVE_DEPTH = 16
MIN_CE_INSTALLATION_FILES = 20

IMPORT_MANIFEST_NAME = ".ce-decky-imported-install.json"

# One recognizable Cheat Engine executable plus the two things every Cheat
# Engine installation carries beside it. Deliberately short: the rest of the
# tree changes between versions, and a list that tracks one exact version would
# reject the next one for no reason.
_CE_EXECUTABLE_NAMES = (
    "cheatengine-x86_64.exe",
    "cheatengine-x86_64-sse4-avx2.exe",
    "cheat engine.exe",
    "cheatengine-i386.exe",
)
_REQUIRED_FILES = ("defines.lua",)
_REQUIRED_DIRECTORIES = ("autorun",)


@dataclass(frozen=True)
class CEArchiveLayout:
    """Where the installation sits inside the archive, and how big it is."""

    root: str
    executable: str
    file_count: int
    total_bytes: int

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _key(name: str) -> str:
    return unicodedata.normalize("NFC", name).rstrip(" .").casefold()


def inspect_ce_archive(source: Path) -> CEArchiveLayout:
    """Locate and validate a Cheat Engine installation inside a .zip archive."""
    try:
        info = source.stat()
    except OSError as exc:
        raise ArchiveImportError(f"archive source is unreadable: {exc}") from exc
    if not source.is_file() or info.st_size <= 0 or info.st_size > MAX_CE_ARCHIVE_BYTES:
        raise ArchiveImportError(
            f"Cheat Engine archive size must be between 1 byte and {MAX_CE_ARCHIVE_BYTES} bytes"
        )
    if source.suffix.lower() != ".zip":
        raise ArchiveImportError("a packed Cheat Engine installation must be a .zip archive")

    try:
        with zipfile.ZipFile(source) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_CE_ARCHIVE_MEMBERS:
                raise ArchiveImportError(
                    f"Cheat Engine archive declares more than {MAX_CE_ARCHIVE_MEMBERS} members"
                )
            files: dict[str, int] = {}
            directories: set[str] = set()
            total = 0
            for item in infos:
                path = _safe_member(item.filename)
                if len(path.parts) > MAX_CE_ARCHIVE_DEPTH:
                    raise ArchiveImportError("Cheat Engine archive member path is nested too deeply")
                if item.is_dir():
                    directories.add("/".join(_key(part) for part in path.parts))
                    if len(files) + len(directories) > MAX_CE_ARCHIVE_TREE_ENTRIES:
                        raise ArchiveImportError("Cheat Engine archive expands to too many files and directories")
                    continue
                if (item.external_attr >> 16) & 0xA000 == 0xA000:
                    raise ArchiveImportError("Cheat Engine archive contains a symbolic link")
                if item.file_size < 0 or item.file_size > MAX_CE_ARCHIVE_MEMBER_BYTES:
                    raise ArchiveImportError("Cheat Engine archive member size is out of range")
                total += item.file_size
                if total > MAX_CE_ARCHIVE_TOTAL_BYTES:
                    raise ArchiveImportError(
                        f"Cheat Engine archive expands to more than {MAX_CE_ARCHIVE_TOTAL_BYTES} bytes"
                    )
                key = "/".join(_key(part) for part in path.parts)
                if key in files:
                    raise ArchiveImportError(f"Cheat Engine archive contains ambiguous member names: {item.filename}")
                files[key] = item.file_size
                for depth in range(1, len(path.parts)):
                    directories.add("/".join(_key(part) for part in path.parts[:depth]))
                if len(files) + len(directories) > MAX_CE_ARCHIVE_TREE_ENTRIES:
                    raise ArchiveImportError("Cheat Engine archive expands to too many files and directories")
    except zipfile.BadZipFile as exc:
        raise ArchiveImportError("Cheat Engine archive is not a readable .zip file") from exc
    except OSError as exc:
        raise ArchiveImportError(f"Cheat Engine archive could not be read: {exc}") from exc

    root, executable = _locate_installation_root(files)
    prefix = f"{root}/" if root else ""
    for required in _REQUIRED_FILES:
        if f"{prefix}{required}" not in files:
            raise ArchiveImportError(
                f"the packed directory is not a Cheat Engine installation: {required} is missing beside the executable"
            )
    for required in _REQUIRED_DIRECTORIES:
        if f"{prefix}{required}" not in directories:
            raise ArchiveImportError(
                f"the packed directory is not a Cheat Engine installation: the {required} directory is missing"
            )
    contained = {key: size for key, size in files.items() if not prefix or key.startswith(prefix)}
    if len(contained) < MIN_CE_INSTALLATION_FILES:
        raise ArchiveImportError(
            f"the packed directory holds {len(contained)} file(s); a Cheat Engine installation has far more. "
            "Pack the whole installation directory, not a selection from it."
        )
    return CEArchiveLayout(
        root=root,
        executable=executable,
        file_count=len(contained),
        total_bytes=sum(contained.values()),
    )


def snapshot_ce_archive(source: Path, staging_root: Path) -> Path:
    """Copy one stable, bounded archive descriptor into plugin-owned staging."""
    if source.suffix.lower() != ".zip":
        raise ArchiveImportError("a packed Cheat Engine installation must be a .zip archive")
    if source.is_symlink():
        raise ArchiveImportError("Cheat Engine archive source must be a regular file")
    staging_root.mkdir(parents=True, exist_ok=True)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    source_fd = -1
    fd = -1
    destination: Path | None = None
    try:
        try:
            source_fd = os.open(source, flags)
        except OSError as exc:
            raise ArchiveImportError("Cheat Engine archive source could not be opened safely") from exc
        fd, name = tempfile.mkstemp(prefix=".ce-archive-source-", suffix=".zip", dir=staging_root)
        destination = Path(name)
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size < 1 or before.st_size > MAX_CE_ARCHIVE_BYTES:
            raise ArchiveImportError(
                f"Cheat Engine archive size must be between 1 byte and {MAX_CE_ARCHIVE_BYTES} bytes"
            )
        total = 0
        with os.fdopen(source_fd, "rb") as reader, os.fdopen(fd, "wb") as writer:
            source_fd = -1
            fd = -1
            while chunk := reader.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_CE_ARCHIVE_BYTES:
                    raise ArchiveImportError("Cheat Engine archive grew beyond the byte limit while being staged")
                writer.write(chunk)
            after = os.fstat(reader.fileno())
            writer.flush()
            os.fsync(writer.fileno())
        try:
            path_after = os.stat(source, follow_symlinks=False)
        except OSError as exc:
            raise ArchiveImportError("Cheat Engine archive changed while being staged") from exc
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        if (
            identity_before != identity_after
            or (path_after.st_dev, path_after.st_ino) != (before.st_dev, before.st_ino)
            or total != before.st_size
        ):
            raise ArchiveImportError("Cheat Engine archive changed while being staged")
        return destination
    except BaseException:
        if source_fd >= 0:
            os.close(source_fd)
        if fd >= 0:
            os.close(fd)
        if destination is not None:
            destination.unlink(missing_ok=True)
        raise


def _locate_installation_root(files: dict[str, int]) -> tuple[str, str]:
    """The shallowest directory holding a Cheat Engine executable, and that name.

    Packing a directory on Windows commonly nests it one or more levels deep, so
    the root is found rather than assumed. The shallowest match wins because a
    Cheat Engine installation can carry further executables below it.
    """
    best: tuple[int, str, str] | None = None
    for key in files:
        path = PurePosixPath(key)
        if path.name not in _CE_EXECUTABLE_NAMES:
            continue
        depth = len(path.parts) - 1
        preference = _CE_EXECUTABLE_NAMES.index(path.name)
        candidate = (depth, str(path.parent) if depth else "", path.name)
        if best is None or (depth, preference) < (best[0], _CE_EXECUTABLE_NAMES.index(best[2])):
            best = candidate
    if best is None:
        names = ", ".join(_CE_EXECUTABLE_NAMES)
        raise ArchiveImportError(
            f"no Cheat Engine executable found in the archive; expected one of: {names}"
        )
    return best[1], best[2]


def extract_ce_archive(source: Path, layout: CEArchiveLayout, stage: Path) -> tuple[int, int]:
    """Write the installation under `layout.root` into an empty staging directory."""
    if stage.exists():
        raise ArchiveImportError("Cheat Engine staging directory already exists")
    stage.mkdir(parents=True)
    prefix = f"{layout.root}/" if layout.root else ""
    written = 0
    total = 0
    try:
        with zipfile.ZipFile(source) as archive:
            for item in archive.infolist():
                if item.is_dir():
                    continue
                path = _safe_member(item.filename)
                key = "/".join(_key(part) for part in path.parts)
                if prefix and not key.startswith(prefix):
                    continue
                # The casefolded key decides what belongs to the installation;
                # the file is written under the name the archive actually
                # carries. Extracting under the folded name would rewrite
                # `Cheat Engine.exe` and every mixed-case asset beside it.
                depth = len(PurePosixPath(prefix[:-1]).parts) if prefix else 0
                relative = path.parts[depth:]
                if not relative:
                    continue
                destination = stage.joinpath(*relative)
                # The staging root is created here and holds nothing else, so a
                # resolved path outside it can only come from a member this
                # module failed to reject. Fail closed rather than write it.
                if not str(destination.resolve()).startswith(f"{stage.resolve()}{os.sep}"):
                    raise ArchiveImportError(f"unsafe archive member path: {item.filename}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as reader, open(destination, "wb", opener=_exclusive_opener) as writer:
                    remaining = item.file_size
                    while remaining > 0:
                        chunk = reader.read(min(remaining, 1024 * 1024))
                        if not chunk:
                            raise ArchiveImportError(f"archive member ended early: {item.filename}")
                        writer.write(chunk)
                        remaining -= len(chunk)
                    if reader.read(1):
                        raise ArchiveImportError(f"archive member is larger than declared: {item.filename}")
                written += 1
                total += item.file_size
    except zipfile.BadZipFile as exc:
        raise ArchiveImportError("Cheat Engine archive is not a readable .zip file") from exc
    if (written, total) != (layout.file_count, layout.total_bytes):
        raise ArchiveImportError("Cheat Engine archive changed between inspection and extraction")
    return written, total


def _exclusive_opener(path: str, flags: int) -> int:
    return os.open(path, flags | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)


def promote_imported_installation(
    stage: Path,
    imported_root: Path,
    archive_sha256: str,
    layout: CEArchiveLayout,
) -> ImportedCE:
    """Validate the staged tree and move it to its final plugin-owned path."""
    inspected = inspect_ce_selection(str(stage))
    atomic_write_json(stage / IMPORT_MANIFEST_NAME, {
        "schema": 1,
        "materialization": "user-archive-import",
        "archive_sha256": archive_sha256,
        "executable": Path(inspected.executable).name,
        "executable_sha256": inspected.sha256,
        "file_count": layout.file_count + 1,
        "total_bytes": layout.total_bytes,
    })
    imported_root.mkdir(parents=True, exist_ok=True)
    destination = imported_root / inspected.sha256
    previous: Path | None = None
    if destination.exists() or destination.is_symlink():
        previous = imported_root / f".{inspected.sha256}.previous-{uuid.uuid4().hex}"
        os.replace(destination, previous)
        fsync_directory(imported_root)
    try:
        os.replace(stage, destination)
        fsync_directory(imported_root)
        promoted = validate_imported_installation(destination)
    except BaseException:
        if previous is not None:
            failed: Path | None = None
            if destination.exists() or destination.is_symlink():
                failed = imported_root / f".{inspected.sha256}.failed-{uuid.uuid4().hex}"
                os.replace(destination, failed)
                fsync_directory(imported_root)
            os.replace(previous, destination)
            fsync_directory(imported_root)
            if failed is not None:
                _discard(failed)
        elif destination.exists() or destination.is_symlink():
            _discard(destination)
        raise
    if previous is not None:
        _discard(previous)
        fsync_directory(imported_root)
    return promoted


def validate_imported_installation(root: Path) -> ImportedCE:
    """Re-check a promoted archive import before anything relies on it."""
    if root.is_symlink():
        raise ValueError("imported Cheat Engine installation root is unsafe")
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise ValueError("imported Cheat Engine installation is missing") from exc
    metadata = load_json(resolved / IMPORT_MANIFEST_NAME, None, max_bytes=64 * 1024)
    expected_keys = {
        "schema", "materialization", "archive_sha256",
        "executable", "executable_sha256", "file_count", "total_bytes",
    }
    if (
        not isinstance(metadata, dict)
        or set(metadata) != expected_keys
        or metadata.get("schema") != 1
        or metadata.get("materialization") != "user-archive-import"
        or not isinstance(metadata.get("archive_sha256"), str)
        or len(metadata["archive_sha256"]) != 64
        or any(character not in "0123456789abcdef" for character in metadata["archive_sha256"])
        or isinstance(metadata.get("file_count"), bool)
        or not isinstance(metadata.get("file_count"), int)
        or not 1 <= metadata["file_count"] <= MAX_CE_ARCHIVE_MEMBERS + 1
        or isinstance(metadata.get("total_bytes"), bool)
        or not isinstance(metadata.get("total_bytes"), int)
        or not 1 <= metadata["total_bytes"] <= MAX_CE_ARCHIVE_TOTAL_BYTES
        or not isinstance(metadata.get("executable"), str)
        or "/" in metadata["executable"] or "\\" in metadata["executable"]
        or metadata["executable"] in {".", ".."}
    ):
        raise ValueError("imported Cheat Engine installation manifest is missing or invalid")
    before = _imported_tree_metrics(resolved)
    manifest_size = before[2]
    if metadata["file_count"] != before[0] or metadata["total_bytes"] != before[1] - manifest_size:
        raise ValueError("imported Cheat Engine installation tree no longer matches its manifest")
    inspected = inspect_ce_selection(str(resolved / metadata["executable"]))
    if metadata.get("executable_sha256") != inspected.sha256:
        raise ValueError("imported Cheat Engine installation identity changed")
    if resolved.name != inspected.sha256:
        raise ValueError("imported Cheat Engine installation is not at its own executable identity")
    if _imported_tree_metrics(resolved) != before:
        raise ValueError("imported Cheat Engine installation changed during validation")
    return inspected


def archive_digest(source: Path) -> str:
    digest = sha256()
    with open(source, "rb") as handle:
        total = 0
        while chunk := handle.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_CE_ARCHIVE_BYTES:
                raise ArchiveImportError("Cheat Engine archive exceeds the byte limit")
            digest.update(chunk)
    return digest.hexdigest()


def _imported_tree_metrics(root: Path) -> tuple[int, int, int]:
    """Return bounded file/byte/manifest-size metrics for one owned import."""
    files = 0
    entries = 0
    total = 0
    manifest_size = -1
    seen: set[str] = set()
    for path in root.rglob("*"):
        entries += 1
        if entries > MAX_CE_ARCHIVE_TREE_ENTRIES:
            raise ValueError("imported Cheat Engine installation exceeds the tree entry bound")
        if path.is_symlink():
            raise ValueError("imported Cheat Engine installation contains a symbolic link")
        relative = path.relative_to(root)
        key = "/".join(_key(part) for part in relative.parts)
        if key in seen:
            raise ValueError("imported Cheat Engine installation contains Windows-ambiguous paths")
        seen.add(key)
        try:
            info = path.stat(follow_symlinks=False)
        except OSError as exc:
            raise ValueError("imported Cheat Engine installation is unreadable") from exc
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("imported Cheat Engine installation contains a non-regular file")
        files += 1
        total += info.st_size
        if files > MAX_CE_ARCHIVE_MEMBERS + 1 or total > MAX_CE_ARCHIVE_TOTAL_BYTES + 64 * 1024:
            raise ValueError("imported Cheat Engine installation exceeds its file or byte bound")
        if relative.as_posix() == IMPORT_MANIFEST_NAME:
            manifest_size = info.st_size
    if manifest_size < 1:
        raise ValueError("imported Cheat Engine installation manifest is missing or invalid")
    return files, total, manifest_size


def _discard(path: Path) -> None:
    import shutil

    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            path.unlink()
        except OSError:
            pass
