from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
import os
import re
import shutil
import stat
import uuid
import unicodedata

from .atomic import atomic_write_json, fsync_directory, load_json, read_regular_bytes

DEFAULT_MAX_FILES = 20_000
DEFAULT_MAX_BYTES = 2 * 1024 * 1024 * 1024
BRIDGE_NAME = "000_ce_decky_bridge.lua"
LEGACY_BRIDGE_NAME = "ce_decky_bridge.lua"
MAIN_NAME = "main.lua"
SOURCE_MAIN_NAME = ".ce-decky-source-main.lua"
MANIFEST_NAME = ".ce-decky-runtime.json"
_SHA_RE = re.compile(r"^[0-9a-fA-F]{64}$")
RUNTIME_LAYOUT_VERSION = 2
_MAIN_BOOTSTRAP = (
    "-- Generated CE Decky private-runtime bootstrap.\n"
    f"dofile(getCheatEngineDir() .. [[autorun\\{BRIDGE_NAME}]])\n"
    f"dofile(getCheatEngineDir() .. [[{SOURCE_MAIN_NAME}]])\n"
).encode("utf-8")
_WINDOWS_INVALID_CHARS = frozenset('<>:"/\\|?*')
_WINDOWS_RESERVED_BASENAMES = frozenset({"con", "prn", "aux", "nul", *[f"com{i}" for i in range(1, 10)], *[f"lpt{i}" for i in range(1, 10)]})
_RESERVED_SOURCE_KEYS = frozenset({
    f"autorun/{BRIDGE_NAME}",
    f"autorun/{LEGACY_BRIDGE_NAME}",
    SOURCE_MAIN_NAME,
    MANIFEST_NAME,
})


@dataclass(frozen=True)
class RuntimeMaterialization:
    root: str
    executable: str
    source_executable_sha256: str  # imported CE executable identity
    source_tree_sha256: str  # exact copied CE tree identity before bridge injection
    bridge_sha256: str
    file_count: int
    total_bytes: int

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _manifest_sha(value: object) -> str:
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
        raise ValueError("runtime manifest digest is invalid")
    return value.lower()


def collect_stale_runtimes(managed_ce_root: Path, keep_key: str) -> list[str]:
    """Delete private runtime snapshots for superseded identities.

    The runtime directory is keyed by the exact Cheat Engine executable SHA, the
    complete source-tree SHA and the bridge SHA, so a plugin update that changes
    the bridge, or a reinstalled Cheat Engine, materializes a new sibling rather
    than replacing the old one. Each sibling is close to a full Cheat Engine
    tree - roughly 94 MB - and nothing collected them, so routine updates leaked
    one installation-size copy per identity until the user removed managed CE
    entirely.

    Only the exact current key is kept. A caller must not collect while a Cheat
    Engine it owns may still be executing out of one of these trees, and it is
    the caller's responsibility to prove that first. Anything that is not a
    directory carrying this module's own manifest is left alone rather than
    deleted, so unrelated or partially written state is reported by the ordinary
    validation paths instead of disappearing.
    """
    runtime_parent = managed_ce_root / "runtime"
    if runtime_parent.is_symlink() or not runtime_parent.is_dir():
        return []
    removed: list[str] = []
    try:
        entries = sorted(runtime_parent.iterdir(), key=lambda item: item.name)
    except OSError:
        return []
    for entry in entries:
        if entry.name == keep_key or entry.is_symlink() or not entry.is_dir():
            continue
        manifest_path = entry / MANIFEST_NAME
        if manifest_path.is_symlink() or not manifest_path.is_file():
            continue
        # A reserved filename is not an identity. Load the manifest, require the
        # complete runtime tuple, and require the directory to be the one that
        # tuple names - otherwise a corrupt or externally modified sibling that
        # merely contains a file with this name would be deleted, which is the
        # opposite of the evidence-preserving posture this function claims.
        try:
            manifest = load_json(manifest_path, None, max_bytes=64 * 1024)
        except (OSError, ValueError):
            continue
        if not isinstance(manifest, dict):
            continue
        try:
            expected = _runtime_key(
                _manifest_sha(manifest.get("source_executable_sha256")),
                _manifest_sha(manifest.get("source_tree_sha256")),
                _manifest_sha(manifest.get("bridge_sha256")),
            )
        except (TypeError, ValueError):
            continue
        if expected != entry.name:
            continue
        try:
            shutil.rmtree(entry)
        except OSError:
            continue
        removed.append(entry.name)
    return removed


def validate_runtime_identity_manifest(
    *,
    runtime_parent: Path,
    executable: Path,
    expected_executable_sha256: str | None,
    expected_bridge_sha256: str,
) -> RuntimeMaterialization:
    """Validate the immutable identity tuple of a recovered live runtime.

    Cheat Engine is allowed to dirty its private tree while running, so this is
    intentionally an identity/compatibility check rather than a second complete
    tree fingerprint. It still requires the strict manifest, aggregate directory
    key, exact root/executable paths, and live executable/bridge bytes to agree.
    """
    runtime_parent = runtime_parent.absolute()
    executable = executable.absolute()
    if (
        runtime_parent.is_symlink()
        or not runtime_parent.is_dir()
        or runtime_parent.resolve(strict=True) != runtime_parent
    ):
        raise ValueError("private runtime root is unavailable or unsafe")
    try:
        relative = executable.relative_to(runtime_parent)
    except ValueError as exc:
        raise ValueError("recovered executable is outside the private runtime root") from exc
    if len(relative.parts) < 2:
        raise ValueError("recovered executable does not name a private runtime directory")
    runtime_root = runtime_parent / relative.parts[0]
    manifest_path = runtime_root / MANIFEST_NAME
    if runtime_root.is_symlink() or executable.is_symlink() or manifest_path.is_symlink():
        raise ValueError("recovered runtime identity resolves through a symlink")
    try:
        if runtime_root.resolve(strict=True) != runtime_root or executable.resolve(strict=True) != executable:
            raise ValueError("recovered runtime path identity is not exact")
    except OSError as exc:
        raise ValueError("recovered runtime path identity is unavailable") from exc
    try:
        raw = load_json(manifest_path, None, max_bytes=64 * 1024)
    except (OSError, ValueError) as exc:
        raise ValueError("recovered runtime manifest is unreadable") from exc
    required = {
        "root", "executable", "source_executable_sha256", "source_tree_sha256",
        "bridge_sha256", "file_count", "total_bytes",
    }
    if not isinstance(raw, dict) or set(raw) != required:
        raise ValueError("recovered runtime manifest schema is invalid")
    file_count = raw.get("file_count")
    total_bytes = raw.get("total_bytes")
    if isinstance(file_count, bool) or not isinstance(file_count, int) or file_count <= 0:
        raise ValueError("recovered runtime manifest file count is invalid")
    if isinstance(total_bytes, bool) or not isinstance(total_bytes, int) or total_bytes <= 0:
        raise ValueError("recovered runtime manifest byte count is invalid")
    try:
        source_executable_sha256 = _manifest_sha(raw.get("source_executable_sha256"))
        source_tree_sha256 = _manifest_sha(raw.get("source_tree_sha256"))
        bridge_sha256 = _manifest_sha(raw.get("bridge_sha256"))
    except ValueError as exc:
        raise ValueError("recovered runtime manifest digest is invalid") from exc
    if raw.get("root") != str(runtime_root) or raw.get("executable") != str(executable):
        raise ValueError("recovered runtime manifest path identity is inconsistent")
    expected_key = _runtime_key(source_executable_sha256, source_tree_sha256, bridge_sha256)
    if runtime_root.name != expected_key:
        raise ValueError("recovered runtime directory does not match its identity tuple")
    expected_bridge_sha256 = _manifest_sha(expected_bridge_sha256)
    if bridge_sha256 != expected_bridge_sha256:
        raise ValueError("recovered runtime bridge identity does not match this build")
    if expected_executable_sha256 is not None:
        expected_executable_sha256 = _manifest_sha(expected_executable_sha256)
        if source_executable_sha256 != expected_executable_sha256:
            raise ValueError("recovered runtime executable identity does not match the prepared session")
    if not executable.is_file() or _sha256_file(executable) != source_executable_sha256:
        raise ValueError("recovered runtime executable bytes do not match its manifest")
    bridge_path = runtime_root / "autorun" / BRIDGE_NAME
    if bridge_path.is_symlink() or not bridge_path.is_file() or _sha256_file(bridge_path) != bridge_sha256:
        raise ValueError("recovered runtime bridge bytes do not match its manifest")
    return RuntimeMaterialization(
        root=str(runtime_root),
        executable=str(executable),
        source_executable_sha256=source_executable_sha256,
        source_tree_sha256=source_tree_sha256,
        bridge_sha256=bridge_sha256,
        file_count=file_count,
        total_bytes=total_bytes,
    )


def materialize_private_runtime(
    *,
    source_root: Path,
    source_executable: Path,
    source_executable_sha256: str,
    managed_ce_root: Path,
    bridge_source: Path,
    max_files: int = DEFAULT_MAX_FILES,
    max_bytes: int = DEFAULT_MAX_BYTES,
    allow_replace: bool = False,
) -> RuntimeMaterialization:
    """Create an immutable plugin-owned copy of an imported CE tree.

    The imported CE installation is read-only source input. This primitive is not
    exposed by the bootstrap UI; it exists for the target-gated resident bridge path.

    ``allow_replace`` permits rebuilding an already promoted runtime tree whose
    content no longer matches its exact manifest, which is what a previous
    Cheat Engine run leaves behind because CE writes into its own directory.
    The caller must prove that no Cheat Engine process CE Decky owns is running
    from that tree before allowing it.
    """
    source_root = source_root.expanduser().resolve(strict=True)
    source_executable = source_executable.expanduser().resolve(strict=True)
    bridge_source = bridge_source.expanduser().resolve(strict=True)
    managed_ce_root = managed_ce_root.expanduser().absolute()
    if managed_ce_root.is_symlink() or managed_ce_root.resolve(strict=False) != managed_ce_root:
        raise ValueError("managed CE root resolves through a symlink")
    source_executable_sha256 = source_executable_sha256.lower()

    if not source_root.is_dir():
        raise ValueError("Cheat Engine source root is not a directory")
    if not (source_root / "autorun").is_dir():
        raise ValueError("Cheat Engine source root does not contain the expected autorun directory")
    if not (source_root / MAIN_NAME).is_file():
        raise ValueError("Cheat Engine source root does not contain the expected main.lua")
    if (source_root / "autorun" / BRIDGE_NAME).exists() or (source_root / MANIFEST_NAME).exists():
        raise ValueError("Cheat Engine source root contains a CE Decky-reserved runtime path")
    if not source_executable.is_file():
        raise ValueError("Cheat Engine source executable is not a file")
    if not bridge_source.is_file():
        raise ValueError("Lua bridge source is not a file")
    if len(source_executable_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in source_executable_sha256):
        raise ValueError("source SHA-256 must be a 64-character hexadecimal digest")
    if max_files <= 0 or max_bytes <= 0:
        raise ValueError("runtime materialization limits must be positive")

    try:
        executable_rel = source_executable.relative_to(source_root)
    except ValueError as exc:
        raise ValueError("Cheat Engine executable must be inside the imported source root") from exc

    if _sha256_file(source_executable) != source_executable_sha256:
        raise ValueError("source executable SHA changed before runtime materialization")

    # Cheap source preflight catches obviously unsafe/oversized trees before hashing/copying.
    _preflight_tree(source_root, max_files=max_files, max_bytes=max_bytes)
    source_tree_sha_before, source_file_count, source_total_bytes = _fingerprint_tree(
        source_root,
        max_files=max_files,
        max_bytes=max_bytes,
    )
    bridge_sha = _sha256_file(bridge_source)

    runtime_parent = managed_ce_root / "runtime"
    _ensure_managed_directory(runtime_parent, managed_ce_root, create=True)
    staging_parent = runtime_parent / ".staging"
    _ensure_managed_directory(staging_parent, managed_ce_root, create=True)
    staging = staging_parent / f"{source_executable_sha256}.{bridge_sha}.{uuid.uuid4().hex}"
    staging.mkdir(parents=False, exist_ok=False)

    try:
        _copy_tree_no_links(source_root, staging, max_files=max_files, max_bytes=max_bytes)
        staged_executable = staging / executable_rel
        if _sha256_file(staged_executable) != source_executable_sha256:
            raise ValueError("copied CE executable failed SHA-256 verification")

        source_tree_sha, file_count, total_bytes = _fingerprint_tree(
            staging,
            max_files=max_files,
            max_bytes=max_bytes,
        )
        if (
            source_tree_sha != source_tree_sha_before
            or file_count != source_file_count
            or total_bytes != source_total_bytes
        ):
            raise ValueError("Cheat Engine source tree changed while creating the private runtime snapshot")
        runtime_key = _runtime_key(source_executable_sha256, source_tree_sha, bridge_sha)
        runtime_root = managed_ce_root / "runtime" / runtime_key
        _ensure_managed_directory(runtime_root.parent, managed_ce_root, create=True)
        runtime_executable = runtime_root / executable_rel
        manifest_path = runtime_root / MANIFEST_NAME

        replacing_existing = False
        if runtime_root.exists():
            if runtime_root.is_symlink() or not runtime_root.is_dir():
                raise ValueError("existing managed CE runtime path is not a regular directory")
            materialized = _validate_existing(
                runtime_root=runtime_root,
                runtime_executable=runtime_executable,
                manifest_path=manifest_path,
                source_executable_sha256=source_executable_sha256,
                source_tree_sha256=source_tree_sha,
                bridge_sha256=bridge_sha,
                max_files=max_files,
                max_bytes=max_bytes,
            )
            if materialized is not None:
                shutil.rmtree(staging, ignore_errors=True)
                return materialized
            # Cheat Engine writes into its own installation directory while it
            # runs: the bundled ceshare extension creates
            # ``autorun/ceshare/processlist.txt`` at startup, which permanently
            # invalidates the exact tree fingerprint of an already promoted
            # snapshot. That made every launch after the first fail closed with
            # no way for a normal user to recover. The runtime tree is
            # plugin-owned and fully reconstructible from the verified source
            # snapshot, so rebuild it from the freshly SHA-verified staging
            # instead of reusing unverified content or refusing forever. CE
            # still only ever starts from a tree this call just verified.
            if not allow_replace:
                raise ValueError(
                    "the private Cheat Engine runtime no longer matches its verified snapshot "
                    "and cannot be rebuilt while a Cheat Engine process CE Decky owns is live; "
                    "stop Cheat Engine and try again"
                )
            replacing_existing = True

        autorun = staging / "autorun"
        staged_bridge = autorun / BRIDGE_NAME
        _copy_one_regular_file(bridge_source, staged_bridge, byte_budget=max_bytes)
        if _sha256_file(staged_bridge) != bridge_sha:
            raise ValueError("copied Lua bridge failed SHA-256 verification")

        # CE loads main.lua before enumerating autorun/*.lua, while Win32
        # FindFirst does not promise an alphabetical directory order. Keep the
        # exact source main.lua under a reserved name and install a tiny,
        # deterministic bootstrap that loads the trusted bridge first, then the
        # untouched source script. The source tree fingerprint below is still
        # reconstructed from the preserved original bytes.
        staged_main = staging / MAIN_NAME
        staged_source_main = staging / SOURCE_MAIN_NAME
        main_mode = stat.S_IMODE(staged_main.stat().st_mode)
        os.rename(staged_main, staged_source_main)
        _write_regular_bytes(staged_main, _MAIN_BOOTSTRAP, mode=main_mode)

        materialized = RuntimeMaterialization(
            root=str(runtime_root),
            executable=str(runtime_executable),
            source_executable_sha256=source_executable_sha256,
            source_tree_sha256=source_tree_sha,
            bridge_sha256=bridge_sha,
            file_count=file_count,
            total_bytes=total_bytes,
        )
        atomic_write_json(staging / MANIFEST_NAME, materialized.as_dict())

        _ensure_managed_directory(runtime_root.parent, managed_ce_root, create=True)
        if replacing_existing:
            retired = staging_parent / f"retired.{uuid.uuid4().hex}"
            os.rename(runtime_root, retired)
            try:
                os.rename(staging, runtime_root)
            except Exception:
                os.rename(retired, runtime_root)
                raise
            fsync_directory(runtime_root.parent)
            shutil.rmtree(retired, ignore_errors=True)
        else:
            os.rename(staging, runtime_root)
            fsync_directory(runtime_root.parent)
        return materialized
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _runtime_key(source_executable_sha256: str, source_tree_sha256: str, bridge_sha256: str) -> str:
    """Bind the complete runtime identity without exceeding Windows MAX_PATH.

    Proton's ``run`` route passes the executable through Wine's ``steam.exe``
    proxy.  Keeping the three 64-character digests as nested path components
    crosses the legacy Windows path limit on a normal Steam Deck home and the
    proxy reports file-not-found before CE starts.  One aggregate digest keeps
    the same exact tuple identity while leaving enough path budget for the
    executable name.
    """
    digest = sha256()
    digest.update(f"ce-decky-runtime-layout-{RUNTIME_LAYOUT_VERSION}\0".encode("ascii"))
    for value in (source_executable_sha256, source_tree_sha256, bridge_sha256):
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("runtime identity requires lowercase SHA-256 values")
        digest.update(bytes.fromhex(value))
    return digest.hexdigest()


def _ensure_managed_directory(path: Path, boundary: Path, *, create: bool) -> None:
    boundary_abs = boundary.expanduser().absolute()
    path_abs = path.expanduser().absolute()
    try:
        relative = path_abs.relative_to(boundary_abs)
    except ValueError as exc:
        raise ValueError("managed CE directory escaped managed root") from exc

    # The managed boundary may not exist in unit tests yet. Create it, then verify
    # that neither it nor any component beneath it resolves through a link.
    if not boundary_abs.exists():
        if not create:
            raise ValueError("managed CE root is missing")
        boundary_abs.mkdir(parents=True, exist_ok=True)
    current = boundary_abs
    candidates = [current]
    for part in relative.parts:
        current = current / part
        candidates.append(current)
    for candidate in candidates:
        if candidate.exists() or candidate.is_symlink():
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ValueError(f"managed CE directory is not a regular directory: {candidate.name}")
        else:
            if not create:
                raise ValueError(f"managed CE directory is missing: {candidate.name}")
            candidate.mkdir(exist_ok=False)
        if candidate.resolve(strict=True) != candidate.absolute():
            raise ValueError(f"managed CE directory resolves through a symlink: {candidate.name}")


def _preflight_tree(root: Path, *, max_files: int, max_bytes: int) -> tuple[int, int]:
    file_count = 0
    total_bytes = 0
    seen_windows: dict[str, str] = {}
    for path in _bounded_tree_paths(root, max_entries=max_files * 2):
        rel = path.relative_to(root)
        key = _validate_windows_relative_path(rel, allow_reserved=False)
        previous = seen_windows.get(key)
        if previous is not None and previous != rel.as_posix():
            raise ValueError(f"CE source tree contains Windows/Wine-colliding paths: {previous} and {rel.as_posix()}")
        seen_windows[key] = rel.as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"CE source tree contains a symlink: {rel}")
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"CE source tree contains a non-regular file: {rel}")
        file_count += 1
        total_bytes += info.st_size
        if file_count > max_files:
            raise ValueError(f"CE source tree exceeds file-count limit ({max_files})")
        if total_bytes > max_bytes:
            raise ValueError(f"CE source tree exceeds byte limit ({max_bytes})")
    if file_count == 0:
        raise ValueError("CE source tree is empty")
    return file_count, total_bytes


def _copy_tree_no_links(source_root: Path, staging_root: Path, *, max_files: int, max_bytes: int) -> None:
    copied_files = 0
    copied_bytes = 0
    seen_windows: dict[str, str] = {}
    for path in sorted(_bounded_tree_paths(source_root, max_entries=max_files * 2), key=lambda item: item.relative_to(source_root).as_posix()):
        rel = path.relative_to(source_root)
        key = _validate_windows_relative_path(rel, allow_reserved=False)
        previous = seen_windows.get(key)
        if previous is not None and previous != rel.as_posix():
            raise ValueError(f"Windows/Wine-colliding path appeared after preflight: {previous} and {rel.as_posix()}")
        seen_windows[key] = rel.as_posix()
        target = staging_root / rel
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"symlink appeared after preflight: {rel}")
        if stat.S_ISDIR(info.st_mode):
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"non-regular file appeared after preflight: {rel}")
        copied_files += 1
        if copied_files > max_files:
            raise ValueError(f"CE source tree exceeds file-count limit ({max_files})")
        copied = _copy_one_regular_file(path, target, byte_budget=max_bytes - copied_bytes)
        copied_bytes += copied
        if copied_bytes > max_bytes:
            raise ValueError(f"CE source tree exceeds byte limit ({max_bytes})")


def _copy_one_regular_file(source: Path, target: Path, *, byte_budget: int) -> int:
    if byte_budget < 0:
        raise ValueError("CE source tree exceeds byte limit")
    target.parent.mkdir(parents=True, exist_ok=True)

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        src_fd = os.open(source, flags)
    except OSError as exc:
        raise ValueError(f"failed to open regular source file without following links: {source}") from exc

    total = 0
    try:
        source_stat = os.fstat(src_fd)
        if not stat.S_ISREG(source_stat.st_mode):
            raise ValueError(f"source path is no longer a regular file: {source}")
        with os.fdopen(src_fd, "rb", closefd=False) as src, target.open("xb") as dst:
            while chunk := src.read(1024 * 1024):
                total += len(chunk)
                if total > byte_budget:
                    raise ValueError("CE source tree exceeds byte limit during copy")
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
            os.fchmod(dst.fileno(), stat.S_IMODE(source_stat.st_mode))
    finally:
        os.close(src_fd)
    return total


def _write_regular_bytes(target: Path, payload: bytes, *, mode: int) -> None:
    with target.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        os.fchmod(handle.fileno(), mode)


def _fingerprint_tree(root: Path, *, max_files: int, max_bytes: int) -> tuple[str, int, int]:
    digest = sha256()
    count = 0
    total = 0
    seen_windows: dict[str, str] = {}
    for path in sorted(_bounded_tree_paths(root, max_entries=max_files * 2), key=lambda item: item.relative_to(root).as_posix()):
        rel_path = path.relative_to(root)
        key = _validate_windows_relative_path(rel_path, allow_reserved=False)
        previous = seen_windows.get(key)
        if previous is not None and previous != rel_path.as_posix():
            raise ValueError(f"managed CE snapshot contains Windows/Wine-colliding paths: {previous} and {rel_path.as_posix()}")
        seen_windows[key] = rel_path.as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"managed CE snapshot contains a symlink: {path.relative_to(root)}")
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"managed CE snapshot contains non-regular file: {path.relative_to(root)}")
        rel = path.relative_to(root).as_posix().encode("utf-8")
        file_sha = _sha256_file(path).encode("ascii")
        count += 1
        total += info.st_size
        if count > max_files or total > max_bytes:
            raise ValueError("managed CE snapshot exceeds configured bounds")
        digest.update(len(rel).to_bytes(4, "big"))
        digest.update(rel)
        digest.update(info.st_size.to_bytes(8, "big"))
        digest.update(file_sha)
    return digest.hexdigest(), count, total


def _validate_existing(
    *,
    runtime_root: Path,
    runtime_executable: Path,
    manifest_path: Path,
    source_executable_sha256: str,
    source_tree_sha256: str,
    bridge_sha256: str,
    max_files: int,
    max_bytes: int,
) -> RuntimeMaterialization | None:
    if manifest_path.is_symlink():
        return None
    try:
        raw = load_json(manifest_path, None, max_bytes=64 * 1024)
        required = {"root", "executable", "source_executable_sha256", "source_tree_sha256", "bridge_sha256", "file_count", "total_bytes"}
        if not isinstance(raw, dict) or set(raw) != required:
            return None
        file_count = raw["file_count"]
        total_bytes = raw["total_bytes"]
        if isinstance(file_count, bool) or not isinstance(file_count, int) or file_count < 0:
            return None
        if isinstance(total_bytes, bool) or not isinstance(total_bytes, int) or total_bytes < 0:
            return None
        for key in ("root", "executable", "source_executable_sha256", "source_tree_sha256", "bridge_sha256"):
            if not isinstance(raw[key], str):
                return None
        materialized = RuntimeMaterialization(
            root=raw["root"],
            executable=raw["executable"],
            source_executable_sha256=raw["source_executable_sha256"],
            source_tree_sha256=raw["source_tree_sha256"],
            bridge_sha256=raw["bridge_sha256"],
            file_count=file_count,
            total_bytes=total_bytes,
        )
    except (OSError, KeyError, TypeError, ValueError):
        return None
    if materialized.root != str(runtime_root) or materialized.executable != str(runtime_executable):
        return None
    if (
        materialized.source_executable_sha256 != source_executable_sha256
        or materialized.source_tree_sha256 != source_tree_sha256
        or materialized.bridge_sha256 != bridge_sha256
    ):
        return None
    if not runtime_executable.is_file() or _sha256_file(runtime_executable) != source_executable_sha256:
        return None
    bridge_path = runtime_root / "autorun" / BRIDGE_NAME
    if not bridge_path.is_file() or _sha256_file(bridge_path) != bridge_sha256:
        return None
    main_path = runtime_root / MAIN_NAME
    source_main_path = runtime_root / SOURCE_MAIN_NAME
    try:
        main_bytes = read_regular_bytes(main_path, max_bytes=len(_MAIN_BOOTSTRAP))
    except (OSError, ValueError):
        return None
    if (
        main_bytes != _MAIN_BOOTSTRAP
        or not source_main_path.is_file()
        or source_main_path.is_symlink()
    ):
        return None
    try:
        tree_sha, count, total = _fingerprint_runtime_source(
            runtime_root,
            max_files=max_files,
            max_bytes=max_bytes,
        )
    except (OSError, ValueError):
        return None
    if tree_sha != source_tree_sha256 or count != materialized.file_count or total != materialized.total_bytes:
        return None
    return materialized


def _fingerprint_runtime_source(root: Path, *, max_files: int, max_bytes: int) -> tuple[str, int, int]:
    digest = sha256()
    count = 0
    total = 0
    source_entries: list[tuple[str, int, str]] = []
    ignored = {MANIFEST_NAME, f"autorun/{BRIDGE_NAME}", MAIN_NAME}
    seen_windows: dict[str, str] = {}
    # The runtime adds the bridge, preserved source main and manifest while
    # retaining the source tree's original entry count.
    for path in sorted(_bounded_tree_paths(root, max_entries=max_files * 2 + 3), key=lambda item: item.relative_to(root).as_posix()):
        rel_path = path.relative_to(root)
        rel_text = rel_path.as_posix()
        key = _validate_windows_relative_path(rel_path, allow_reserved=True)
        previous = seen_windows.get(key)
        if previous is not None and previous != rel_text:
            raise ValueError(f"managed CE runtime contains Windows/Wine-colliding paths: {previous} and {rel_text}")
        seen_windows[key] = rel_text
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"managed CE runtime contains a symlink: {rel_text}")
        if stat.S_ISDIR(info.st_mode):
            continue
        if rel_text in ignored:
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"managed CE runtime contains non-regular file: {rel_text}")
        source_rel_text = MAIN_NAME if rel_text == SOURCE_MAIN_NAME else rel_text
        file_sha = _sha256_file(path)
        count += 1
        total += info.st_size
        if count > max_files or total > max_bytes:
            raise ValueError("managed CE runtime exceeds configured bounds")
        source_entries.append((source_rel_text, info.st_size, file_sha))

    # The preserved source main has a reserved runtime filename, so restore its
    # original relative path before sorting and hashing the source identity.
    for source_rel_text, size, file_sha_text in sorted(source_entries):
        rel = source_rel_text.encode("utf-8")
        file_sha = file_sha_text.encode("ascii")
        digest.update(len(rel).to_bytes(4, "big"))
        digest.update(rel)
        digest.update(size.to_bytes(8, "big"))
        digest.update(file_sha)
    return digest.hexdigest(), count, total


def _bounded_tree_paths(root: Path, *, max_entries: int) -> list[Path]:
    """Materialize a deterministic tree only after bounding files and directories."""
    if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries <= 0:
        raise ValueError("CE tree entry limit is invalid")
    paths: list[Path] = []
    for path in root.rglob("*"):
        if len(paths) >= max_entries:
            raise ValueError(f"CE tree exceeds entry limit ({max_entries})")
        paths.append(path)
    return paths



def _validate_windows_relative_path(relative: Path, *, allow_reserved: bool) -> str:
    """Validate one CE tree path against Win32/Wine filename semantics.

    Source trees live on a case-sensitive Linux filesystem but are consumed by a
    Windows application under Wine. Reject names that Windows cannot represent and
    pairs that collapse after Unicode normalization, case folding, or trailing
    dot/space trimming. This avoids copying an ambiguous tree whose meaning changes
    when CE opens it through Wine.
    """
    if relative.is_absolute() or not relative.parts:
        raise ValueError("CE tree path must be relative")
    canonical_parts: list[str] = []
    for part in relative.parts:
        if part in {"", ".", ".."}:
            raise ValueError(f"CE tree contains an unsafe path component: {relative}")
        normalized = unicodedata.normalize("NFC", part)
        if any(ord(ch) < 0x20 or ch in _WINDOWS_INVALID_CHARS for ch in normalized):
            raise ValueError(f"CE tree path is not representable on Windows/Wine: {relative}")
        collapsed = normalized.rstrip(" .")
        if not collapsed:
            raise ValueError(f"CE tree path collapses to an empty Windows/Wine component: {relative}")
        basename = collapsed.split(".", 1)[0].casefold()
        if basename in _WINDOWS_RESERVED_BASENAMES:
            raise ValueError(f"CE tree uses a reserved Windows device name: {relative}")
        canonical_parts.append(collapsed.casefold())
    key = "/".join(canonical_parts)
    if not allow_reserved and key in _RESERVED_SOURCE_KEYS:
        raise ValueError(f"Cheat Engine source root contains a CE Decky-reserved runtime path: {relative}")
    return key


def _sha256_file(path: Path) -> str:
    digest = sha256()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ValueError("CE runtime file could not be opened safely for hashing") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("CE runtime file must be regular")
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest()
