from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
import os
import re
import stat
import subprocess
import tempfile
import selectors
import time
import unicodedata
import zipfile

from .child_env import child_environment
from .text import utf8_bytes

MAX_ARCHIVE_MEMBERS = 2_000
MAX_ARCHIVE_DECLARED_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_FILE_BYTES = 512 * 1024 * 1024
MAX_MEMBER_PATH = 512
MAX_CT_BYTES = 32 * 1024 * 1024
SEVENZIP_TIMEOUT_SECONDS = 30
MAX_7Z_LIST_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_7Z_STDERR_BYTES = 64 * 1024
MAX_ZIP_PASSWORD_BYTES = 1024


class ArchiveImportError(ValueError):
    pass


class ArchiveHasNoTable(ArchiveImportError):
    """The archive was read and holds no `.CT` at all.

    Separate from every other archive failure because it is the only one that is
    a property of these exact bytes rather than of this device or this read: a
    7-Zip without the handler for the format, an unreadable member, a size the
    limits refuse are all conditions that can change, while an archive with no
    table in it will have none the next time either. That is what lets the
    caller retire the row instead of offering the same download again.
    """


@dataclass(frozen=True)
class ArchiveMember:
    path: str
    size: int
    packed_size: int | None
    encrypted: bool
    format: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ArchiveInspection:
    format: str
    members: tuple[ArchiveMember, ...]

    def as_dict(self) -> dict[str, object]:
        return {"format": self.format, "members": [member.as_dict() for member in self.members]}


RAR_UNSUPPORTED_MESSAGE = (
    "the 7-Zip on this device has no RAR handler, so this archive cannot be opened here. "
    "The reduced 7za/7zr builds omit it; installing full 7-Zip (p7zip) adds it."
)

# The archive kinds 7-Zip opens for this plugin, to the format each row and
# member is labelled with. `.rar` reads through exactly the same `l -slt` and
# `x -so` contract as `.7z`; what differs is that only a full 7-Zip build has
# the handler for it, which `sevenzip_opens_rar` is what answers.
SEVENZIP_ARCHIVE_FORMATS = {".7z": "7z", ".7zip": "7z", ".rar": "rar"}
# Formats whose encrypted member no answer from the user can open, because
# 7-Zip takes a password only on its command line.
ARGV_PASSWORD_FORMATS = frozenset({"7z", "rar"})


def argv_password_refusal(archive_format: str) -> str:
    """The refusal for an encrypted member of a format only 7-Zip opens.

    Composed here because the caller that has to recognise it reads the sentence
    rather than a type: `TableStore` turns every archive failure into a plain
    `ValueError`, and a refusal recognised by a hard-coded `.7z` stopped
    recognising `.rar` the moment that format was added, which put the user back
    in front of a password prompt no answer can satisfy.
    """
    return f"password-protected .{archive_format} import is not enabled"


# The answer for one observed binary identity. Asked from `inspect_archive` and
# again from `extract_ct_member`, and the extraction lists the archive a second
# time on top, so one `.rar` import ran 7-Zip four times without this. A path is
# not an identity: package updates replace `/usr/bin/7z` in place while this
# backend remains loaded.
_RAR_SUPPORT: dict[tuple[str, int, int, int, int, int], bool] = {}


def _sevenzip_identity(sevenzip: str) -> tuple[str, int, int, int, int, int] | None:
    """A cache identity that changes when a resolved executable is replaced."""
    try:
        resolved = Path(sevenzip).resolve(strict=True)
        info = resolved.stat()
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or not os.access(resolved, os.X_OK):
        return None
    return (
        str(resolved), info.st_dev, info.st_ino, info.st_size,
        info.st_mtime_ns, info.st_ctime_ns,
    )


def sevenzip_opens_rar(sevenzip: str | None) -> bool:
    """Whether this exact 7-Zip binary has a RAR handler.

    Asked rather than assumed: `7z` carries it and the reduced `7za`/`7zr`
    builds, which `_find_7zip` falls back to, do not - and they answer `i` with
    a successful exit and a format list that simply lacks it, so the exit code
    settles nothing. Getting this wrong the optimistic way costs the user a
    provider countdown and a download to be told what was knowable at search
    time; getting it wrong the pessimistic way hides rows a host can open.
    """
    if not sevenzip:
        return False
    identity = _sevenzip_identity(sevenzip)
    if identity is None:
        return False
    cached = _RAR_SUPPORT.get(identity)
    if cached is not None:
        return cached
    result = _run_7z([sevenzip, "i"])
    if result.returncode != 0:
        # A successful listing without Rar is proof that the build lacks the
        # handler. A command that failed is only a failed probe, so let search's
        # advisory wrapper fail open and let an actual import report this error
        # rather than falsely naming the build as reduced.
        detail = result.stderr.decode("utf-8", "replace").strip()[-500:]
        raise ArchiveImportError(
            f"7-Zip capability probe failed (code {result.returncode}): {detail}"
        )
    listing = result.stdout.decode("utf-8", "replace")
    # `i` prints a table whose format column carries the handler's own name, and
    # a build without the handler prints no row for it at all. The name is
    # matched as a whole word rather than by column, because the table's columns
    # are laid out differently in the format and codec sections of the same
    # output and the reduced builds print neither.
    supported = re.search(r"\bRar5?\b", listing) is not None
    # Do not attach the answer to an executable replaced while it was running.
    # The answer is still valid for this call, and the next call probes again.
    if _sevenzip_identity(sevenzip) == identity:
        for stale in tuple(_RAR_SUPPORT):
            if stale[0] == identity[0] and stale != identity:
                _RAR_SUPPORT.pop(stale, None)
        _RAR_SUPPORT[identity] = supported
    return supported


def inspect_archive(source: Path, sevenzip: str | None = None) -> ArchiveInspection:
    try:
        info = source.stat()
    except OSError as exc:
        raise ArchiveImportError(f"archive source is unreadable: {exc}") from exc
    if not source.is_file() or info.st_size <= 0 or info.st_size > MAX_ARCHIVE_FILE_BYTES:
        raise ArchiveImportError(f"archive file size must be between 1 byte and {MAX_ARCHIVE_FILE_BYTES} bytes")
    suffix = source.suffix.lower()
    if suffix == ".zip":
        return _inspect_zip(source)
    archive_format = SEVENZIP_ARCHIVE_FORMATS.get(suffix)
    if archive_format is not None:
        if not sevenzip:
            raise ArchiveImportError(f"7-Zip is required to inspect .{archive_format} archives")
        if archive_format == "rar" and not sevenzip_opens_rar(sevenzip):
            raise ArchiveImportError(RAR_UNSUPPORTED_MESSAGE)
        return _inspect_7z(source, sevenzip, archive_format)
    raise ArchiveImportError("supported table archives are .zip, .7z and .rar")


def extract_ct_member(
    source: Path,
    member_path: str,
    destination: Path,
    *,
    sevenzip: str | None = None,
    password: str | None = None,
) -> None:
    suffix = source.suffix.lower()
    if suffix == ".zip":
        _extract_zip_member(source, member_path, destination, password=password)
        return
    archive_format = SEVENZIP_ARCHIVE_FORMATS.get(suffix)
    if archive_format is not None:
        if password:
            # 7z accepts passwords only on the process command line. Do not leak them via
            # argv/process listings. Password-protected 7z can be added after a target-safe
            # credential transport is proven, and .rar goes through the same 7-Zip.
            raise ArchiveImportError(argv_password_refusal(archive_format))
        if not sevenzip:
            raise ArchiveImportError(f"7-Zip is required to extract .{archive_format} archives")
        if archive_format == "rar" and not sevenzip_opens_rar(sevenzip):
            raise ArchiveImportError(RAR_UNSUPPORTED_MESSAGE)
        _extract_7z_member(source, member_path, destination, sevenzip, archive_format)
        return
    raise ArchiveImportError("supported table archives are .zip, .7z and .rar")


def _safe_member(name: str) -> PurePosixPath:
    if "\x00" in name:
        raise ArchiveImportError("archive member path contains NUL")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
        raise ArchiveImportError("archive member path contains control characters")
    if any(unicodedata.bidirectional(ch) in {"RLE", "LRE", "RLO", "LRO", "PDF", "RLI", "LRI", "FSI", "PDI"} for ch in name):
        raise ArchiveImportError("archive member path contains bidirectional control characters")
    normalized = name.replace("\\", "/")
    if len(normalized) > MAX_MEMBER_PATH:
        raise ArchiveImportError("archive member path is too long")
    # PurePosixPath collapses repeated separators and '.' components. Inspect
    # raw normalized segments first so two visually different archive names can
    # never silently canonicalize to the same selection path. A single trailing
    # slash is reserved for a directory entry and is allowed here.
    raw_segments = normalized[:-1].split("/") if normalized.endswith("/") else normalized.split("/")
    if not raw_segments or any(part in {"", ".", ".."} for part in raw_segments):
        raise ArchiveImportError(f"unsafe archive member path: {name}")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ArchiveImportError(f"unsafe archive member path: {name}")
    if path.parts and path.parts[0].endswith(":"):
        raise ArchiveImportError(f"unsafe archive member path: {name}")
    return path


def _collision_key(path: PurePosixPath) -> str:
    """Canonical UI/filesystem collision key for untrusted archive names.

    The selected CT is extracted as a single content-addressed blob, not as the
    archive hierarchy. Still reject names that differ only by Unicode
    normalization or case so selection cannot be visually/filesystem ambiguous
    across Linux/Windows tooling.
    """
    return "/".join(unicodedata.normalize("NFC", part).rstrip(" .").casefold() for part in path.parts)


def _validate_ct_member(path: PurePosixPath, size: int) -> None:
    if path.suffix.lower() != ".ct":
        return
    if size <= 0 or size > MAX_CT_BYTES:
        raise ArchiveImportError(f".CT archive member must be between 1 and {MAX_CT_BYTES} bytes")


def _inspect_zip(source: Path) -> ArchiveInspection:
    try:
        with zipfile.ZipFile(source) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_MEMBERS:
                raise ArchiveImportError("archive contains too many members")
            total = 0
            seen: set[str] = set()
            candidates: list[ArchiveMember] = []
            for info in infos:
                safe = _safe_member(info.filename)
                canonical = safe.as_posix()
                collision = _collision_key(safe)
                if collision in seen:
                    raise ArchiveImportError(f"archive contains duplicate or ambiguous normalized path: {canonical}")
                seen.add(collision)
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise ArchiveImportError(f"archive symlink member is rejected: {canonical}")
                if info.is_dir():
                    continue
                if info.file_size < 0 or info.compress_size < 0:
                    raise ArchiveImportError("archive contains invalid member sizes")
                total += info.file_size
                if total > MAX_ARCHIVE_DECLARED_BYTES:
                    raise ArchiveImportError("archive declared extraction size is too large")
                _validate_ct_member(safe, info.file_size)
                if safe.suffix.lower() == ".ct":
                    candidates.append(
                        ArchiveMember(
                            path=canonical,
                            size=info.file_size,
                            packed_size=info.compress_size,
                            encrypted=bool(info.flag_bits & 0x1),
                            format="zip",
                        )
                    )
            if not candidates:
                raise ArchiveHasNoTable("archive contains no .CT files")
            return ArchiveInspection("zip", tuple(candidates))
    except zipfile.BadZipFile as exc:
        raise ArchiveImportError(f"invalid ZIP archive: {exc}") from exc


def _extract_zip_member(source: Path, member_path: str, destination: Path, *, password: str | None) -> None:
    inspection = _inspect_zip(source)
    matches = [member for member in inspection.members if member.path == member_path]
    if len(matches) != 1:
        raise ArchiveImportError("selected .CT member is not uniquely present in archive")
    selected = matches[0]
    if password is not None:
        pwd = utf8_bytes(password, "ZIP password")
        if len(pwd) > MAX_ZIP_PASSWORD_BYTES:
            raise ArchiveImportError(f"ZIP password exceeds {MAX_ZIP_PASSWORD_BYTES} UTF-8 bytes")
    else:
        pwd = None
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(source) as archive:
            info = next(info for info in archive.infolist() if _safe_member(info.filename).as_posix() == member_path)
            with archive.open(info, "r", pwd=pwd) as reader:
                _stream_bounded(reader, destination, expected_size=selected.size)
    except RuntimeError as exc:
        message = str(exc).lower()
        if "password" in message or "encrypted" in message:
            raise ArchiveImportError("ZIP member requires a correct password") from exc
        raise ArchiveImportError(f"ZIP extraction failed: {exc}") from exc
    except zipfile.BadZipFile as exc:
        raise ArchiveImportError(f"ZIP extraction failed: {exc}") from exc


def _sevenzip_env() -> dict[str, str]:
    # `child_environment` is what makes this run at all under Decky's loader:
    # the bundle's `LD_LIBRARY_PATH` reached `/usr/bin/7z`, which is a `/bin/sh`
    # wrapper, and the shell died on an undefined symbol before 7-Zip started.
    env = child_environment()
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    return env


def _run_7z(args: list[str], *, timeout: int = SEVENZIP_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    """Run a metadata-only 7z command with bounded captured output."""
    try:
        process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_sevenzip_env(),
            bufsize=0,
        )
    except OSError as exc:
        raise ArchiveImportError(f"failed to execute 7-Zip: {exc}") from exc
    if process.stdout is None or process.stderr is None:
        process.kill(); process.wait()
        raise ArchiveImportError("failed to capture 7-Zip output")
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    deadline = time.monotonic() + timeout
    stdout = bytearray()
    stderr = bytearray()
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ArchiveImportError("7-Zip operation timed out")
            events = selector.select(timeout=min(0.5, remaining))
            if not events:
                if process.poll() is not None:
                    continue
                continue
            for key, _ in events:
                chunk = os.read(key.fileobj.fileno(), 1024 * 1024)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                target = stdout if key.data == "stdout" else stderr
                limit = MAX_7Z_LIST_OUTPUT_BYTES if key.data == "stdout" else MAX_7Z_STDERR_BYTES
                if len(target) + len(chunk) > limit:
                    raise ArchiveImportError(f"7-Zip {key.data} exceeds capture limit")
                target.extend(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ArchiveImportError("7-Zip operation timed out")
        try:
            code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise ArchiveImportError("7-Zip operation timed out") from exc
        return subprocess.CompletedProcess(args, code, bytes(stdout), bytes(stderr))
    except Exception:
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.terminate(); process.wait(timeout=2)
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()


def _inspect_7z(source: Path, sevenzip: str, archive_format: str = "7z") -> ArchiveInspection:
    result = _run_7z([sevenzip, "l", "-slt", "-ba", "-sccUTF-8", "--", str(source)])
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()[-500:]
        raise ArchiveImportError(f"7-Zip list failed (code {result.returncode}): {detail}")
    try:
        text = result.stdout.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise ArchiveImportError("7-Zip list output is not valid UTF-8") from exc
    records = _parse_7z_slt(text)
    if len(records) > MAX_ARCHIVE_MEMBERS:
        raise ArchiveImportError("archive contains too many members")
    total = 0
    seen: set[str] = set()
    candidates: list[ArchiveMember] = []
    for record in records:
        raw_path = record.get("Path")
        if not raw_path:
            continue
        safe = _safe_member(raw_path)
        canonical = safe.as_posix()
        collision = _collision_key(safe)
        if collision in seen:
            raise ArchiveImportError(f"archive contains duplicate or ambiguous normalized path: {canonical}")
        seen.add(collision)
        folder = record.get("Folder", "-").strip() == "+" or canonical.endswith("/")
        if folder:
            continue
        attributes = record.get("Attributes", "").strip()
        if record.get("Symbolic Link", "").strip() or attributes[:1].lower() == "l":
            raise ArchiveImportError(f"archive symlink member is rejected: {canonical}")
        try:
            size = int(record.get("Size", "-1"))
        except ValueError as exc:
            raise ArchiveImportError(f"invalid 7z member size for {canonical}") from exc
        packed_text = record.get("Packed Size", "").strip()
        try:
            packed = int(packed_text) if packed_text else None
        except ValueError as exc:
            raise ArchiveImportError(f"invalid 7z packed size for {canonical}") from exc
        if size < 0 or (packed is not None and packed < 0):
            raise ArchiveImportError(f"invalid 7z member size for {canonical}")
        total += size
        if total > MAX_ARCHIVE_DECLARED_BYTES:
            raise ArchiveImportError("archive declared extraction size is too large")
        _validate_ct_member(safe, size)
        if safe.suffix.lower() == ".ct":
            candidates.append(
                ArchiveMember(
                    path=canonical,
                    size=size,
                    packed_size=packed,
                    encrypted=record.get("Encrypted", "-").strip() == "+",
                    format=archive_format,
                )
            )
    if not candidates:
        raise ArchiveHasNoTable("archive contains no .CT files")
    return ArchiveInspection(archive_format, tuple(candidates))


def _parse_7z_slt(text: str) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r\n")
        if not line.strip():
            if current:
                records.append(current)
                current = {}
            continue
        if " = " not in line:
            continue
        key, value = line.split(" = ", 1)
        key = key.strip()
        if key in current:
            raise ArchiveImportError(f"7-Zip metadata record contains duplicate field: {key}")
        current[key] = value
    if current:
        records.append(current)
    return records


def _extract_7z_member(
    source: Path, member_path: str, destination: Path, sevenzip: str, archive_format: str = "7z",
) -> None:
    inspection = _inspect_7z(source, sevenzip, archive_format)
    matches = [member for member in inspection.members if member.path == member_path]
    if len(matches) != 1:
        raise ArchiveImportError("selected .CT member is not uniquely present in archive")
    selected = matches[0]
    if selected.encrypted:
        raise ArchiveImportError(argv_password_refusal(archive_format))
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".part", dir=destination.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            _stream_7z_stdout(
                [sevenzip, "x", "-so", "-bd", "-bb0", "--", str(source), member_path],
                handle,
                expected_size=selected.size,
            )
            handle.flush()
            os.fsync(handle.fileno())
        actual = temp.stat().st_size
        if actual != selected.size or actual <= 0 or actual > MAX_CT_BYTES:
            raise ArchiveImportError("7-Zip extracted size does not match preflight metadata")
        os.replace(temp, destination)
        _fsync_dir(destination.parent)
    finally:
        temp.unlink(missing_ok=True)


def _stream_7z_stdout(args: list[str], writer, *, expected_size: int) -> None:
    """Stream 7z stdout with a hard byte limit and wall-clock timeout.

    `subprocess.run(..., stdout=file)` would let a malicious archive write far
    beyond its declared size before post-extraction validation. Pipes let the
    plugin enforce the selected member's preflight size while extraction is in
    progress. Both stdout and stderr are drained to avoid a child-process
    deadlock.
    """
    try:
        process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_sevenzip_env(),
            bufsize=0,
        )
    except OSError as exc:
        raise ArchiveImportError(f"failed to execute 7-Zip: {exc}") from exc

    if process.stdout is None or process.stderr is None:
        process.kill()
        process.wait()
        raise ArchiveImportError("failed to capture 7-Zip output")

    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    deadline = time.monotonic() + SEVENZIP_TIMEOUT_SECONDS
    total = 0
    stderr = bytearray()
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ArchiveImportError("7-Zip operation timed out")
            events = selector.select(timeout=min(0.5, remaining))
            if not events:
                if process.poll() is not None:
                    # Pipes can still contain buffered data; keep selecting until EOF.
                    continue
                continue
            for key, _ in events:
                try:
                    chunk = os.read(key.fileobj.fileno(), 1024 * 1024)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stdout":
                    total += len(chunk)
                    if total > expected_size or total > MAX_CT_BYTES:
                        raise ArchiveImportError("7-Zip output exceeds preflight size limit")
                    writer.write(chunk)
                elif len(stderr) < 64 * 1024:
                    stderr.extend(chunk[: 64 * 1024 - len(stderr)])
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ArchiveImportError("7-Zip operation timed out")
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise ArchiveImportError("7-Zip operation timed out") from exc
        if returncode != 0:
            detail = bytes(stderr).decode("utf-8", "replace").strip()[-500:]
            raise ArchiveImportError(f"7-Zip extraction failed (code {returncode}): {detail}")
        if total != expected_size:
            raise ArchiveImportError("7-Zip extracted size does not match preflight metadata")
    except Exception:
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=2)
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()


def _stream_bounded(reader, destination: Path, *, expected_size: int) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".part", dir=destination.parent)
    temp = Path(temp_name)
    total = 0
    try:
        with os.fdopen(fd, "wb") as writer:
            while True:
                chunk = reader.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > expected_size or total > MAX_CT_BYTES:
                    raise ArchiveImportError("extracted .CT exceeds preflight size limit")
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        if total != expected_size or total <= 0:
            raise ArchiveImportError("extracted .CT size does not match archive metadata")
        os.replace(temp, destination)
        _fsync_dir(destination.parent)
    finally:
        temp.unlink(missing_ok=True)


def _fsync_dir(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
