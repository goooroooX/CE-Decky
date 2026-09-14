from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
import asyncio
import os
import re
import signal
import shutil
import stat
import subprocess
import time
import unicodedata
import uuid
from urllib.parse import urlsplit

from .activity_log import log_activity, log_failure
from .atomic import atomic_write_json, fsync_directory, load_json, read_proc_bytes, read_regular_bytes
from .authenticode import SignatureFacts, verify_publisher
from .child_env import child_environment
from .ce77_extractor import extract_reviewed_installer
from .ce_import import ImportedCE, inspect_ce_selection
from .ce_installer_source import FallbackPolicy, ResolvedInstaller, resolve_installer
from .ce_runtime import _validate_windows_relative_path
from .network import DownloadDestinationError, NetworkClient, NetworkError, ResponseTooLarge
from .operations import drain_through_cancellation
from .pe_version import read_pe_version


MANIFEST_PATH = Path(__file__).with_name("managed_ce_manifest.json")
INSTALL_TIMEOUT_SECONDS = 15 * 60
MAX_INSTALL_FILES = 8192
MAX_INSTALL_TREE_ENTRIES = 16_384
MAX_INSTALL_BYTES = 2 * 1024 * 1024 * 1024
MAX_INSTALL_LOG_BYTES = 1024 * 1024
OUTPUT_DRAIN_SECONDS = 5
PROCESS_RETIRE_TIMEOUT_SECONDS = 5
PROCESS_RETIRE_POLL_SECONDS = 0.1
INSTALLER_JOURNAL_SCHEMA = 1
MAX_STEAM_VDF_BYTES = 2 * 1024 * 1024
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_LIBRARY_PATH_RE = re.compile(r'"path"\s+"((?:\\.|[^"\\])*)"')
_BIDI_CONTROLS = frozenset({"RLE", "LRE", "RLO", "LRO", "PDF", "RLI", "LRI", "FSI", "PDI"})


class PublisherReleaseUnsupported(ValueError):
    """The link serves a different Cheat Engine release this build cannot read.

    Separate from an untrustworthy download: the artifact proved it came from
    Cheat Engine's own publisher, and the only thing standing in the way is
    that the native extractor is pinned to the exact reviewed artifact's
    format. Refusing is correct; blaming the download would not be.

    The signature proves the publisher, not the ordering. Nothing here reads a
    version out of an artifact it has already refused to parse, so this says a
    *different* release rather than a newer one.
    """


class RediscoveryError(ValueError):
    """The current installer URL could not be read from the download page.

    Typed rather than recognized by its wording, because the extractor's own
    failures mention the same installer format and would otherwise be reported
    as though the website were at fault.
    """


@dataclass(frozen=True)
class ManagedCERelease:
    visible_version: str
    artifact_filename: str
    artifact_url: str
    allowed_hosts: tuple[str, ...]
    sha256: str
    size: int
    max_size: int
    # Kept only for in-memory compatibility with pre-0.13 test/operation
    # records. Native extraction never reads or executes installer arguments.
    silent_arguments: tuple[str, ...]
    reviewed_at: str
    reviewed_authenticode_subject: str
    #: SHA-256 of the reviewed publisher's SubjectPublicKeyInfo. It does not
    #: authorize an install - only the reviewed hash does - but it tells an
    #: upstream release this build cannot extract apart from a substituted
    #: artifact, which are very different things to report.
    publisher_key_sha256: str = ""
    rediscovery: FallbackPolicy | None = None

    def public(self) -> dict[str, object]:
        return {
            "visible_version": self.visible_version,
            "artifact_filename": self.artifact_filename,
            "sha256": self.sha256,
            "size": self.size,
            "reviewed_at": self.reviewed_at,
            "rediscovery_available": self.rediscovery is not None,
        }


@dataclass(frozen=True)
class ProtonTool:
    tool_id: str
    name: str
    path: str
    proton_sha256: str
    source: str

    def public(self) -> dict[str, object]:
        return asdict(self)


def load_release_manifest(path: Path = MANIFEST_PATH) -> ManagedCERelease:
    raw = load_json(path, None, max_bytes=64 * 1024)
    required = {
        "schema", "visible_version", "artifact_filename", "artifact_url", "allowed_hosts",
        "sha256", "size", "max_size", "reviewed_at",
        "reviewed_authenticode_subject", "publisher_key_sha256", "rediscovery",
    }
    if not isinstance(raw, dict) or set(raw) != required or raw.get("schema") != 3:
        raise ValueError("managed CE release manifest is malformed")
    url = raw["artifact_url"]
    hosts = raw["allowed_hosts"]
    digest = raw["sha256"]
    if not isinstance(url, str) or len(url.encode("utf-8")) > 8192:
        raise ValueError("managed CE artifact URL is invalid")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
        or parsed.password is not None or parsed.port not in (None, 443)
        or parsed.fragment or parsed.query
    ):
        raise ValueError("managed CE artifact URL is invalid")
    if (
        not isinstance(hosts, list) or not hosts
        or any(not isinstance(item, str) or not re.fullmatch(r"[a-z0-9.-]+", item) for item in hosts)
        or len(set(hosts)) != len(hosts) or parsed.hostname.casefold() not in hosts
    ):
        raise ValueError("managed CE host policy is invalid")
    if not isinstance(digest, str) or not _SHA_RE.fullmatch(digest):
        raise ValueError("managed CE artifact SHA-256 is invalid")
    size, max_size = raw["size"], raw["max_size"]
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (size, max_size)) or not 1 <= size <= max_size <= 64 * 1024 * 1024:
        raise ValueError("managed CE artifact size policy is invalid")
    strings = (raw["visible_version"], raw["artifact_filename"], raw["reviewed_at"], raw["reviewed_authenticode_subject"])
    if any(not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 512 for value in strings):
        raise ValueError("managed CE release metadata is invalid")
    if PurePosixPath(raw["artifact_filename"]).name != raw["artifact_filename"] or not raw["artifact_filename"].lower().endswith(".exe"):
        raise ValueError("managed CE artifact filename is invalid")
    publisher_key = raw["publisher_key_sha256"]
    if not isinstance(publisher_key, str) or not _SHA_RE.fullmatch(publisher_key):
        raise ValueError("managed CE publisher key pin is invalid")
    return ManagedCERelease(
        raw["visible_version"], raw["artifact_filename"], url,
        tuple(host.casefold() for host in hosts), digest, size, max_size, (),
        raw["reviewed_at"], raw["reviewed_authenticode_subject"],
        publisher_key_sha256=publisher_key,
        rediscovery=_load_rediscovery_policy(raw["rediscovery"], max_size),
    )


def _load_rediscovery_policy(raw: object, max_size: int) -> FallbackPolicy | None:
    """Read the bounded permission to rediscover a rotated artifact URL.

    `null` disables rediscovery outright, which keeps the reviewed pinned URL
    the only route a build can be shipped with.
    """
    if raw is None:
        return None
    required = {"page_url", "page_hosts", "max_page_bytes", "max_helper_bytes"}
    if not isinstance(raw, dict) or set(raw) != required:
        raise ValueError("managed CE rediscovery policy is malformed")
    page_url, hosts = raw["page_url"], raw["page_hosts"]
    if not isinstance(page_url, str) or len(page_url.encode("utf-8")) > 8192:
        raise ValueError("managed CE rediscovery page URL is invalid")
    parsed = urlsplit(page_url)
    if (
        parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
        or parsed.password is not None or parsed.port not in (None, 443)
        or parsed.fragment or parsed.query
    ):
        raise ValueError("managed CE rediscovery page URL is invalid")
    if (
        not isinstance(hosts, list) or not hosts
        or any(not isinstance(item, str) or not re.fullmatch(r"[a-z0-9.-]+", item) for item in hosts)
        or len(set(hosts)) != len(hosts) or parsed.hostname.casefold() not in hosts
    ):
        raise ValueError("managed CE rediscovery host policy is invalid")
    page_bytes, helper_bytes = raw["max_page_bytes"], raw["max_helper_bytes"]
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (page_bytes, helper_bytes)):
        raise ValueError("managed CE rediscovery byte policy is invalid")
    # The helper is a whole installer of its own, so it gets the artifact bound
    # rather than a second number nobody would keep in step with it.
    if not 1024 <= page_bytes <= 8 * 1024 * 1024 or not 1024 <= helper_bytes <= max_size:
        raise ValueError("managed CE rediscovery byte policy is invalid")
    return FallbackPolicy(
        page_url=page_url,
        page_hosts=tuple(host.casefold() for host in hosts),
        max_page_bytes=page_bytes,
        max_stub_bytes=helper_bytes,
    )


def discover_steam_library_roots(user_home: Path) -> tuple[list[Path], list[Path]]:
    """Discover current Steam client roots and library roots for one user home.

    Both the managed installer (Proton tool identity) and the plugin-owned CE
    launcher (exact ``steamapps/compatdata`` prefix) need the same read-only
    discovery, so it stays one implementation. Unreadable or non-absolute
    library declarations are skipped, never guessed.
    """
    home = user_home.resolve(strict=True)
    steam_roots: set[Path] = set()
    for candidate in (home / ".local/share/Steam", home / ".steam/steam", home / ".steam/root"):
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if resolved.is_dir():
            steam_roots.add(resolved)

    libraries: set[Path] = set(steam_roots)
    for root in tuple(steam_roots):
        vdf = root / "steamapps/libraryfolders.vdf"
        try:
            data = read_regular_bytes(vdf, max_bytes=MAX_STEAM_VDF_BYTES, allow_missing=True)
            text = data.decode("utf-8") if data else ""
        except (ValueError, UnicodeError):
            continue
        for match in _LIBRARY_PATH_RE.finditer(text):
            value = match.group(1).replace(r"\\", "\\").replace(r'\"', '"')
            path = Path(value)
            if not path.is_absolute():
                continue
            try:
                resolved = path.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if resolved.is_dir():
                libraries.add(resolved)
    return sorted(steam_roots, key=str), sorted(libraries, key=str)


def discover_proton_tools(user_home: Path) -> list[ProtonTool]:
    """Discover exact installed Proton scripts; the controller chooses one identity."""
    steam_roots, libraries = discover_steam_library_roots(user_home)

    candidates: list[tuple[Path, str]] = []
    for library in libraries:
        common = library / "steamapps/common"
        if common.is_dir():
            for item in _bounded_directories(common, 512):
                if item.name.casefold().startswith("proton") and (item / "proton").is_file():
                    candidates.append((item, "steam-library"))
    for root in steam_roots:
        custom = root / "compatibilitytools.d"
        if custom.is_dir():
            for item in _bounded_directories(custom, 256):
                if (item / "proton").is_file():
                    candidates.append((item, "compatibilitytools.d"))

    tools: dict[str, ProtonTool] = {}
    for root, source in candidates:
        try:
            resolved = root.resolve(strict=True)
            script = read_regular_bytes(resolved / "proton", max_bytes=16 * 1024 * 1024)
            assert script is not None
        except (OSError, RuntimeError, ValueError):
            continue
        if not os.access(resolved / "proton", os.X_OK) or _unsafe_text(root.name, 256) or _unsafe_text(str(resolved), 4096):
            continue
        script_sha = sha256(script).hexdigest()
        tool_id = sha256((str(resolved) + "\0" + script_sha).encode("utf-8")).hexdigest()
        tools[tool_id] = ProtonTool(tool_id, root.name, str(resolved), script_sha, source)
    return sorted(tools.values(), key=lambda item: (item.name.casefold(), item.path.casefold()))


def managed_ce_capability(user_home: Path | None = None) -> dict[str, object]:
    try:
        release = load_release_manifest()
        error = None
    except ValueError as exc:
        release, error = None, str(exc)
    return _release_capability(release, error)


def _release_capability(release: "ManagedCERelease | None", error: str | None) -> dict[str, object]:
    return {
        "schema": 3,
        "mode": "managed_install",
        "managed_install_available": release is not None,
        "release_manifest_loaded": release is not None,
        "network_download_enabled": release is not None,
        "native_extraction_enabled": release is not None,
        "reason": error or "Download and extract the reviewed Windows Cheat Engine release locally; Proton is used only when launching Cheat Engine.",
        "release": release.public() if release else None,
    }


class ManagedCEManager:
    def __init__(self, user_home: Path, ce_root: Path, temp_root: Path, network: NetworkClient, logger) -> None:
        self.user_home, self.ce_root, self.temp_root = user_home, ce_root, temp_root
        self.network, self.logger = network, logger
        # A malformed plugin-shipped manifest must degrade the managed installer
        # only. Raising here would abort backend load and take the controller-
        # accessible manual import fallback down with it.
        try:
            self.release: ManagedCERelease | None = load_release_manifest()
            self.release_error: str | None = None
        except ValueError as exc:
            self.release, self.release_error = None, str(exc)
        self._operation: dict[str, object] | None = None
        self._task: asyncio.Task[None] | None = None
        self._closing = False

    def capability(self) -> dict[str, object]:
        # The release identity shown to the user and the release identity that
        # authorizes the install must be the same immutable object. Re-reading
        # the packaged manifest here meant a plugin update landing between
        # backend construction and a capability refresh could describe release B
        # while `start()` still verified and registered cached release A. One
        # backend instance uses one snapshot; a reload adopts the new one whole.
        capability = _release_capability(self.release, self.release_error)
        capability["operation"] = dict(self._operation) if self._operation is not None else None
        return capability

    def _require_release(self) -> ManagedCERelease:
        if self.release is None:
            raise ValueError(self.release_error or "managed CE release manifest is unavailable")
        return self.release

    async def start(self, *, force: bool = False) -> dict[str, object]:
        if self._closing:
            raise RuntimeError("managed CE installer is closing")
        if self._task is not None and not self._task.done():
            raise ValueError("a managed CE install is already running")
        if not isinstance(force, bool):
            raise ValueError("managed CE reinstall flag must be a boolean")
        release = self._require_release()
        operation_id = uuid.uuid4().hex
        destination = self.ce_root / "installations" / release.sha256
        try:
            installed = validate_managed_installation(destination, self.ce_root, release)
        except ValueError:
            installed = None
        if installed is not None and not force:
            self._operation = {
                "operation_id": operation_id, "state": "completed", "progress": None,
                "message": "Existing reviewed managed Cheat Engine installation is ready",
                "error": None,
                "installed": installed.as_dict(),
                "provenance": read_managed_provenance(Path(installed.root)),
            }
            self._task = None
            log_activity(
                self.logger, "info", "managed_ce.reused",
                operation=operation_id[:12], release_sha=release.sha256[:12],
            )
            return self.status(operation_id)
        self._operation = {
            "operation_id": operation_id, "state": "downloading", "progress": None,
            "message": (
                "Downloading a fresh reviewed Cheat Engine artifact for reinstall"
                if force else "Downloading the reviewed Cheat Engine artifact"
            ),
            "error": None,
            "installed": None,
            "provenance": None,
        }
        log_activity(
            self.logger, "info", "managed_ce.started",
            operation=operation_id[:12], force=force,
            release_sha=release.sha256[:12], expected_bytes=release.size,
        )
        self._task = asyncio.create_task(self._run(operation_id, force=force))
        return self.status(operation_id)

    #: An operation in any other state is still writing into `ce_root`.
    TERMINAL_OPERATION_STATES = frozenset({"completed", "failed", "cancelled"})

    def has_active_operation(self) -> bool:
        """Whether an install is still downloading into or extracting under `ce_root`.

        Deleting that tree from under the extractor destroys a half-written
        installation and leaves this manager reporting an operation whose files
        are gone, so the destructive path asks before it starts.
        """
        if self._task is not None and not self._task.done():
            return True
        operation = self._operation
        if operation is None:
            return False
        return str(operation.get("state")) not in self.TERMINAL_OPERATION_STATES

    def status(self, operation_id: str) -> dict[str, object]:
        if self._operation is None or self._operation.get("operation_id") != operation_id:
            raise ValueError("managed CE install operation is unknown")
        return dict(self._operation)

    async def wait(self, operation_id: str) -> None:
        self.status(operation_id)
        task = self._task
        if task is not None and not task.done():
            # asyncio.wait resolves without re-raising the worker's outcome. A
            # cancelled or failed install must surface as an inspectable operation
            # state, never as cancellation of the caller's RPC.
            await asyncio.wait({task})

    def completed_install(self, operation_id: str) -> ImportedCE:
        release = self._require_release()
        status = self.status(operation_id)
        installed = status.get("installed")
        if status["state"] != "completed" or not isinstance(installed, dict) or not isinstance(installed.get("root"), str):
            raise ValueError("managed CE install has not completed successfully")
        return validate_managed_installation(Path(installed["root"]), self.ce_root, release)

    def consume_completed(self, operation_id: str) -> None:
        """Forget one completed operation after its installation is registered.

        The manager intentionally keeps a completed operation visible until the
        service persists the resulting CE identity.  Once that commit succeeds,
        retaining the operation would make a frontend reload keep monitoring a
        setup that was already registered and would allow a later start to
        overwrite evidence that was never explicitly consumed.
        """
        self.completed_install(operation_id)
        if self._task is not None and not self._task.done():
            raise ValueError("managed CE install is still running")
        self._operation = None
        self._task = None

    async def cancel(self, operation_id: str) -> dict[str, object]:
        self.status(operation_id)
        log_activity(self.logger, "info", "managed_ce.cancel_requested", operation=operation_id[:12])
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        result = self.status(operation_id)
        log_activity(
            self.logger, "info", "managed_ce.cancel_settled",
            operation=operation_id[:12], state=result.get("state"),
        )
        return result

    async def close(self) -> None:
        self._closing = True
        running = self._task is not None and not self._task.done()
        # Written before the wait, because a wait is what Decky's five second
        # stop budget kills without a record. An install is the one owner here
        # that legitimately takes minutes, so a close during one is expected to
        # be slow and has to be able to say that it is.
        log_activity(self.logger, "info", "managed_ce.close_started", install_running=running)
        if running:
            self._task.cancel()
            # The install task drains its own extraction, promotion and cleanup
            # workers. Abandoning the wait would orphan exactly those.
            if await drain_through_cancellation(
                asyncio.gather(self._task, return_exceptions=True),
                label="managed_ce.install", logger=self.logger, watching=(self._task,),
            ):
                raise asyncio.CancelledError

    async def _obtain_installer(
        self,
        operation_id: str,
        release: ManagedCERelease,
        work: Path,
        *,
        force: bool,
    ) -> tuple[ManagedCERelease, dict[str, object], Path]:
        """Put a verified installer in the owned cache, by whichever route works.

        The reviewed URL is always tried first, and its SHA-256 is the whole
        answer while it serves the reviewed bytes. Only once that route fails -
        a dead link, a refused or oversized response, or bytes that are not the
        reviewed artifact - does rediscovery run, and what it finds is held to
        that same hash and nothing else. Rediscovery changes where the artifact
        is looked for; it never changes what may be installed.
        """
        cached = self._cached_installer(release, force=force)
        if cached is not None:
            installer, provenance = cached
            return release, provenance, installer

        downloaded = work / release.artifact_filename
        try:
            await self.network.download(
                release.artifact_url, downloaded,
                allowed_hosts=frozenset(release.allowed_hosts), max_bytes=release.max_size,
            )
            _verify_installer(downloaded, release)
        except asyncio.CancelledError:
            raise
        # Only a failure of the download route itself is a reason to look for
        # another one. A local write or permission failure would fail the same
        # way again, so it is reported rather than turned into a second
        # request against a site the user did not ask to contact.
        except DownloadDestinationError:
            # The local file could not be opened. A different URL would fail
            # exactly the same way, so this must not become a request to a site
            # the user never asked to contact.
            raise
        except (NetworkError, ValueError) as exc:
            if release.rediscovery is None:
                raise
            log_activity(
                self.logger, "info", "managed_ce.reviewed_url_failed",
                operation=operation_id[:12], detail=_safe_status_text(str(exc), 256),
            )
            self._set(
                operation_id,
                message="The reviewed download link did not answer; looking up the current one",
            )
            downloaded.unlink(missing_ok=True)
            return await self._rediscover_installer(operation_id, release, work)

        log_activity(
            self.logger, "info", "managed_ce.artifact_verified",
            operation=operation_id[:12], release_sha=release.sha256[:12],
            bytes=release.size, refreshed=force,
        )
        provenance = _provenance(release, source="reviewed-url")
        return release, provenance, self._cache(release, downloaded, provenance)

    async def _rediscover_installer(
        self,
        operation_id: str,
        release: ManagedCERelease,
        work: Path,
    ) -> tuple[ManagedCERelease, dict[str, object], Path]:
        """Find the current artifact when the reviewed link no longer serves it."""
        policy = release.rediscovery
        assert policy is not None
        helpers = work / "rediscovery"
        _ensure_managed_directory(helpers, self.temp_root, create=True)
        try:
            resolved = await resolve_installer(self.network, policy, helpers)
        except asyncio.CancelledError:
            raise
        except (NetworkError, ResponseTooLarge, ValueError) as exc:
            raise RediscoveryError(str(exc)) from exc
        log_activity(
            self.logger, "info", "managed_ce.rediscovered_url",
            operation=operation_id[:12], host=resolved.host,
            helper_host=resolved.stub_host, filename=resolved.filename,
        )
        self._set(operation_id, message="Downloading the current Cheat Engine artifact")

        downloaded = work / resolved.filename
        # The host allowlist comes from the artifact URL the pinned download
        # page led to, so a redirect cannot widen it mid-transfer. Failures of
        # this transfer belong to the rediscovered route, not to the reviewed
        # link that already failed above.
        try:
            await self.network.download(
                resolved.url, downloaded,
                allowed_hosts=frozenset({resolved.host}), max_bytes=release.max_size,
            )
        except DownloadDestinationError:
            raise
        except (NetworkError, ResponseTooLarge) as exc:
            raise RediscoveryError(str(exc)) from exc

        data = read_regular_bytes(downloaded, max_bytes=release.max_size)
        if data is None:
            raise ValueError("rediscovered Cheat Engine artifact could not be read back")
        if len(data) < 64 or data[:2] != b"MZ":
            raise ValueError("managed CE installer is not a PE/MZ executable")

        # The reviewed SHA-256 is the only thing that authorizes an install:
        # the native extractor is pinned to this exact artifact's format, so
        # bytes it cannot parse cannot become a Cheat Engine installation
        # however trustworthy their origin. The publisher signature is read
        # anyway, because "the link serves another release from this publisher"
        # and "we were handed a different file" need very different answers.
        signature: SignatureFacts | None = None
        signature_note: str | None = None
        try:
            signature = await verify_publisher(downloaded, release.publisher_key_sha256, data=data)
        except asyncio.CancelledError:
            raise
        except (ValueError, OSError) as exc:
            signature_note = _safe_status_text(str(exc), 256)

        if sha256(data).hexdigest() != release.sha256 or len(data) != release.size:
            if signature is not None:
                raise PublisherReleaseUnsupported(
                    "the link now serves a different Cheat Engine release than the "
                    "one this version can extract"
                )
            raise ValueError(
                "rediscovered Cheat Engine artifact is not the reviewed release and "
                f"its signature could not be trusted: {signature_note}"
            )

        log_activity(
            self.logger, "info", "managed_ce.artifact_verified",
            operation=operation_id[:12], release_sha=release.sha256[:12],
            bytes=len(data), rediscovered=True,
            signer=(signature.common_name if signature else None),
        )
        provenance = _provenance(
            release, source="rediscovered", resolved=resolved, signature=signature,
            signature_note=signature_note,
        )
        return release, provenance, self._cache(release, downloaded, provenance)

    def _cached_installer(
        self, release: ManagedCERelease, *, force: bool,
    ) -> tuple[Path, dict[str, object]] | None:
        """The reviewed artifact already in the owned cache, if it is intact.

        Its provenance is whatever the download that filled the cache recorded.
        Reuse is a timing detail, not a route, so inventing one here would let
        a rediscovered artifact be shown afterwards as though the reviewed link
        had served it.
        """
        installer = self.ce_root / "installers" / release.sha256 / release.artifact_filename
        _ensure_managed_directory(installer.parent, self.ce_root, create=True)
        if force or not (installer.exists() or installer.is_symlink()):
            return None
        try:
            _verify_installer(installer, release)
        except (OSError, ValueError):
            # The cache is plugin-owned and content-addressed. Recover from an
            # interrupted or corrupt prior download without asking the Game Mode
            # user to repair files by hand.
            installer.unlink(missing_ok=True)
            (installer.parent / INSTALLER_SOURCE_NAME).unlink(missing_ok=True)
            fsync_directory(installer.parent)
            return None
        recorded = load_json(installer.parent / INSTALLER_SOURCE_NAME, None, max_bytes=64 * 1024)
        if _valid_provenance(recorded):
            return installer, dict(recorded)
        return installer, _provenance(release, source="cache")

    def _cache(
        self, release: ManagedCERelease, downloaded: Path, provenance: dict[str, object],
    ) -> Path:
        """Move a verified download into the content-addressed owned cache.

        A forced reinstall deliberately refreshes the source, so a prior cache
        is kept until this independently verified download can atomically
        replace it; an interrupted refresh cannot turn a usable cache into an
        absent one.

        The record is cleared before the artifact moves and written after it
        lands, so no interruption can leave provenance describing an
        acquisition other than the one in the cache. Every window in between
        reads as "route not recorded", which is true, where the opposite
        ordering would have stated a route confidently and wrongly.
        """
        installer = self.ce_root / "installers" / release.sha256 / release.artifact_filename
        source = installer.parent / INSTALLER_SOURCE_NAME
        _ensure_managed_directory(installer.parent, self.ce_root, create=True)
        source.unlink(missing_ok=True)
        fsync_directory(installer.parent)
        os.replace(downloaded, installer)
        fsync_directory(installer.parent)
        atomic_write_json(source, provenance)
        fsync_directory(installer.parent)
        return installer

    async def _run(self, operation_id: str, *, force: bool = False) -> None:
        release = self._require_release()
        work = self.temp_root / "managed-ce" / operation_id
        stage = work / "tree"
        try:
            # The per-operation working directory holds the installer staging tree
            # on every path, not only when a download happens. Validate it before
            # the cached branch can create `stage` through an unchecked component.
            _ensure_managed_directory(work, self.temp_root, create=True)
            release, provenance, installer = await self._obtain_installer(
                operation_id, release, work, force=force
            )
            self._set(
                operation_id, state="extracting",
                message="Extracting the reviewed Cheat Engine files locally",
                provenance=provenance,
            )
            extraction = asyncio.create_task(asyncio.to_thread(
                extract_reviewed_installer, installer, stage,
                expected_size=release.size, expected_sha256=release.sha256,
            ))
            # Native extraction mutates only the owned per-operation tree, but its
            # worker cannot be interrupted safely. Drain it before cleanup so
            # cancellation never races a filesystem writer.
            if await drain_through_cancellation(
                extraction, label="managed_ce.extraction", logger=self.logger, watching=(extraction,),
            ):
                raise asyncio.CancelledError
            extraction.result()
            log_activity(self.logger, "info", "managed_ce.extraction_completed", operation=operation_id[:12])
            self._set(operation_id, state="verifying", message="Verifying the extracted Cheat Engine tree")
            finalize = asyncio.create_task(asyncio.to_thread(
                _promote_installation, stage, self.ce_root, release,
                replace_valid=force, provenance=provenance,
            ))
            # Directory promotion is a bounded local mutation. Cancellation
            # cannot stop its worker thread, so drain it before cleanup or
            # unload instead of racing rmtree against an in-flight promote.
            cancelled_after_commit_point = await drain_through_cancellation(
                finalize, label="managed_ce.promotion", logger=self.logger, watching=(finalize,),
            )
            installed = finalize.result()
            message = "Managed Cheat Engine is ready"
            if cancelled_after_commit_point:
                message += "; cancellation arrived after the irreversible promotion commit point"
            self._set(operation_id, state="completed", message=message, installed=installed.as_dict())
            log_activity(
                self.logger, "info", "managed_ce.completed",
                operation=operation_id[:12], executable_sha=installed.sha256[:12],
                late_cancel=cancelled_after_commit_point,
            )
        except asyncio.CancelledError:
            self._set(operation_id, state="cancelled", message="Managed Cheat Engine installation cancelled")
            log_activity(self.logger, "info", "managed_ce.cancelled", operation=operation_id[:12])
            raise
        except Exception as exc:
            detail = _managed_setup_error(exc)
            log_failure(
                self.logger, "managed_ce.failed", exc,
                expected=isinstance(exc, (NetworkError, ResponseTooLarge, OSError, ValueError)),
                operation=operation_id[:12], detail=detail,
            )
            self._set(operation_id, state="failed", message="Managed Cheat Engine installation failed", error=detail)
        finally:
            # The reviewed installer cache and promoted installation live
            # outside this UUID work directory. Extraction/finalization workers
            # have been drained on every path above, so the owned transaction
            # tree can now be removed without racing a writer.
            cleanup = asyncio.create_task(asyncio.to_thread(
                _cleanup_managed_operation_work, work, self.temp_root,
            ))
            try:
                if await drain_through_cancellation(
                    cleanup, label="managed_ce.cleanup", logger=self.logger, watching=(cleanup,),
                ):
                    raise asyncio.CancelledError
                cleanup.result()
            except (OSError, ValueError) as exc:
                log_failure(
                    self.logger, "managed_ce.cleanup_failed", exc, expected=True,
                    operation=operation_id[:12],
                )
    # Legacy Proton-installer recovery is intentionally absent from the
    # 0.13 managed path. Existing schema-1 installed trees remain readable
    # below so a non-forced start is non-destructive; a forced reinstall
    # converges them to native extraction.

    def _set(self, operation_id: str, **values: object) -> None:
        if self._operation is not None and self._operation.get("operation_id") == operation_id:
            self._operation.update(values)


def _managed_process_group_state(pgid: int, marker: str, *, proc_root: Path = Path("/proc")) -> str:
    """Return ``matched``, ``gone`` or ``unreadable`` for an installer PGID.

    A PID alone can be recycled and a Proton leader can exit before Wine. Every
    visible member must therefore still carry the journal's unguessable marker.
    """
    if os.name != "posix" or pgid <= 1 or not re.fullmatch(r"[0-9a-f]{32}", marker):
        return "unreadable"
    try:
        entries = sorted((entry for entry in os.scandir(proc_root) if entry.name.isdigit()), key=lambda entry: int(entry.name))
    except OSError:
        return "unreadable"
    if len(entries) > MAX_INSTALL_FILES:
        return "unreadable"
    found = False
    for entry in entries:
        root = Path(entry.path)
        try:
            stat_raw = read_proc_bytes(root / "stat", max_bytes=4096)
        except (OSError, ValueError):
            return "unreadable"
        if stat_raw is None:
            continue
        try:
            stat_text = stat_raw.decode("ascii")
            fields = stat_text[stat_text.rfind(")") + 1:].split()
            member_pgid = int(fields[2])
        except (UnicodeDecodeError, ValueError, IndexError):
            return "unreadable"
        if member_pgid != pgid:
            continue
        try:
            environ = read_proc_bytes(root / "environ", max_bytes=64 * 1024)
        except (OSError, ValueError):
            return "unreadable"
        if environ is None:
            # A process can disappear between the bounded /proc snapshot and
            # its environment read during TERM/KILL polling. Re-check the same
            # PID before treating the observation as ambiguous.
            try:
                still_present = read_proc_bytes(root / "stat", max_bytes=4096)
            except (OSError, ValueError):
                return "unreadable"
            if still_present is None:
                continue
            return "unreadable"
        if not environ:
            return "unreadable"
        values: dict[str, str] = {}
        for part in environ.split(b"\0"):
            try:
                name, value = part.split(b"=", 1)
                values[name.decode("ascii")] = value.decode("utf-8")
            except (UnicodeDecodeError, ValueError):
                continue
        if values.get("CE_DECKY_MANAGED_INSTALLER") != marker:
            return "unreadable"
        found = True
    return "matched" if found else "gone"


async def _retire_managed_process_group(pgid: int, marker: str) -> None:
    state = _managed_process_group_state(pgid, marker)
    if state == "gone":
        return
    if state != "matched":
        raise RuntimeError("managed installer process group ownership is ambiguous")
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    for _ in range(int(PROCESS_RETIRE_TIMEOUT_SECONDS / PROCESS_RETIRE_POLL_SECONDS)):
        await asyncio.sleep(PROCESS_RETIRE_POLL_SECONDS)
        state = _managed_process_group_state(pgid, marker)
        if state == "gone":
            return
        if state != "matched":
            raise RuntimeError("managed installer process group ownership became ambiguous")
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    for _ in range(int(PROCESS_RETIRE_TIMEOUT_SECONDS / PROCESS_RETIRE_POLL_SECONDS)):
        await asyncio.sleep(PROCESS_RETIRE_POLL_SECONDS)
        state = _managed_process_group_state(pgid, marker)
        if state == "gone":
            return
        if state != "matched":
            raise RuntimeError("managed installer process group ownership became ambiguous")
    raise RuntimeError("managed installer process group did not exit")


def _wineserver_stop_command(tool: ProtonTool, prefix: Path, ce_root: Path) -> tuple[Path, dict[str, str]] | None:
    """The exact `wineserver` and environment that retires one owned prefix.

    Shared by the two callers rather than written twice: the ordinary
    asynchronous retirement below, and the synchronous one the unload prologue
    takes, which has no event loop to await a subprocess on. Every check that
    decides whether this may run at all lives here, so neither caller can be the
    one that skips it.

    Returns `None` when there is nothing to retire, which is a prefix Proton has
    not created yet. Malformed state still fails closed.
    """
    _verify_proton_tool(tool)
    _ensure_managed_directory(prefix, ce_root, create=False)
    wine_prefix = prefix / "pfx"
    # Proton creates `pfx` asynchronously while it initializes a cold prefix, so
    # an early cancellation or a plugin unload can reach here before it exists.
    # There is then no wineserver to stop and nothing to retire, and treating
    # that as a cleanup failure turned a successful process stop back into an
    # error. Malformed state stays fail-closed: the path must still be inside
    # the owned boundary, and anything that does exist must be a plain
    # directory rather than a symlink or a file.
    if wine_prefix.is_symlink():
        raise ValueError("managed CE installer path contains a symlink")
    if not wine_prefix.exists():
        try:
            wine_prefix.absolute().relative_to(ce_root.resolve(strict=True))
        except ValueError as exc:
            raise ValueError("managed CE installer path escaped its owned root") from exc
        return None
    _ensure_managed_directory(wine_prefix, ce_root, create=False)
    tool_root = Path(tool.path).resolve(strict=True)
    try:
        wineserver = (tool_root / "files/bin/wineserver").resolve(strict=True)
        wineserver.relative_to(tool_root)
    except (OSError, ValueError) as exc:
        raise RuntimeError("selected Proton wineserver is unavailable or unsafe") from exc
    if not wineserver.is_file() or not os.access(wineserver, os.X_OK):
        raise RuntimeError("selected Proton wineserver is unavailable or unsafe")
    env = os.environ.copy()
    for key in tuple(env):
        if key.startswith(("STEAM_COMPAT_", "PROTON_")) or key in {
            "SteamAppId", "SteamGameId", "WINEPREFIX", "WINEDLLOVERRIDES", "WINEDEBUG",
        }:
            env.pop(key, None)
    # Decky's loader is a bundle whose own library path must not reach a system
    # binary; `wineserver` is one.
    env = child_environment(env)
    env.update({"WINEPREFIX": str(wine_prefix), "WINEDEBUG": "-all"})
    return wineserver, env


def retire_owned_proton_prefix_now(tool: ProtonTool, prefix: Path, ce_root: Path, budget: float) -> str:
    """Retire one owned prefix without an event loop, inside a bounded window.

    The unload prologue is where an owned self-test has to be stopped on this
    host, and it has no loop to await a subprocess on: `docs/FIELD_NOTES.md`
    records that Decky's stop starves it and SIGKILLs the process five seconds
    later. Signalling the launch's own process group is not enough on its own,
    which is what the asynchronous retirement below exists to say: Cheat Engine
    reaches its own process group under Proton, with `init` as its parent, and
    outlives the group that started it. That was measured on the device.

    Best effort by construction, and it says which: the caller records the
    answer rather than assuming the prefix went.
    """
    try:
        command = _wineserver_stop_command(tool, prefix, ce_root)
    except (OSError, RuntimeError, ValueError):
        return "unavailable"
    if command is None:
        return "absent"
    wineserver, env = command
    deadline = time.monotonic() + budget
    for argument in ("-k", "-w"):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "timeout"
        try:
            finished = subprocess.run(  # noqa: S603 - exact validated executable inside the owned tool
                [str(wineserver), argument], env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=remaining, check=False,
                # Its own session, like the asynchronous path: this runs while
                # the plugin is being stopped, and the retirement must not be
                # reachable by anything aimed at the process doing it.
                start_new_session=True,
            )
        except subprocess.TimeoutExpired:
            return "timeout"
        except OSError:
            return "unavailable"
        # `wineserver -k` may report failure when the server exits before it can
        # acknowledge the kill request. `-w` is the authoritative confirmation:
        # it returns only after that prefix's server has gone.
        if finished.returncode != 0 and argument != "-k":
            return "refused"
    return "retired"


async def _retire_owned_proton_prefix(tool: ProtonTool, prefix: Path, ce_root: Path) -> None:
    """Stop Wine clients that escaped Proton's original POSIX process group."""
    built = _wineserver_stop_command(tool, prefix, ce_root)
    if built is None:
        return
    wineserver, env = built
    for argument in ("-k", "-w"):
        command = await asyncio.create_subprocess_exec(
            str(wineserver), argument, env=env,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            await asyncio.wait_for(command.wait(), timeout=PROCESS_RETIRE_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            command.kill()
            await command.wait()
            raise RuntimeError("owned Proton prefix did not stop") from exc
        # `wineserver -k` may report failure when the server exits before it
        # can acknowledge the kill request.  `-w` is the authoritative
        # confirmation: it returns only after that prefix's server has gone.
        if command.returncode != 0 and argument != "-k":
            raise RuntimeError("owned Proton prefix could not be stopped")


def validate_managed_installation(root: Path, ce_root: Path, release: ManagedCERelease | None = None) -> ImportedCE:
    """Validate one promoted managed installation.

    The tree is content-addressed by its own directory name, so integrity is
    settled against that rather than against whatever the packaged manifest
    currently pins - an unreadable or updated manifest cannot invalidate an
    installation whose bytes are still exactly what promoted them. "Is this the
    release this build pins" is a separate, freshness question, and callers that
    mean it pass the release explicitly.
    """
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise ValueError("managed Cheat Engine installation is missing") from exc
    artifact_sha = resolved.name
    if (
        _SHA_RE.fullmatch(artifact_sha) is None
        or (release is not None and artifact_sha != release.sha256)
    ):
        raise ValueError("configured managed Cheat Engine root is not the reviewed installation")
    try:
        boundary = (ce_root / "installations" / artifact_sha).resolve(strict=True)
    except OSError as exc:
        raise ValueError("managed Cheat Engine installation is missing") from exc
    if resolved != boundary:
        raise ValueError("configured managed Cheat Engine root is not the reviewed installation")
    before = _validate_install_tree(resolved)
    metadata = load_json(resolved / ".ce-decky-managed-install.json", None, max_bytes=64 * 1024)
    metadata_keys = {
        "schema", "artifact_sha256", "visible_version", "executable", "executable_sha256",
        "materialization", "file_count", "total_bytes", "provenance",
    }
    pre_provenance_keys = metadata_keys - {"provenance"}
    legacy_metadata_keys = {
        "schema", "artifact_sha256", "visible_version", "executable", "executable_sha256",
        "proton_tool_id", "proton_name", "file_count", "total_bytes",
    }
    native_metadata = (
        isinstance(metadata, dict)
        and (
            (
                set(metadata) == metadata_keys and metadata.get("schema") == 3
                and _valid_provenance(metadata.get("provenance"))
            )
            # Promoted before provenance was recorded; still exactly valid.
            or (set(metadata) == pre_provenance_keys and metadata.get("schema") == 2)
        )
        and metadata.get("materialization") == "native-ce77-extraction"
    )
    legacy_metadata = (
        isinstance(metadata, dict) and set(metadata) == legacy_metadata_keys
        and metadata.get("schema") == 1
        and isinstance(metadata.get("proton_tool_id"), str)
        and _SHA_RE.fullmatch(metadata["proton_tool_id"]) is not None
        and isinstance(metadata.get("proton_name"), str)
        and not _unsafe_text(metadata["proton_name"], 256)
    )
    if (
        not (native_metadata or legacy_metadata)
        or metadata.get("artifact_sha256") != artifact_sha
        or not isinstance(metadata.get("visible_version"), str)
        or _unsafe_text(metadata["visible_version"], 128)
        or not isinstance(metadata.get("executable"), str)
        or _unsafe_text(metadata["executable"], 256)
        or metadata["executable"] in {".", ".."}
        or "/" in metadata["executable"]
        or "\\" in metadata["executable"]
        or metadata.get("file_count") != before[0]
        or isinstance(metadata.get("total_bytes"), bool) or not isinstance(metadata.get("total_bytes"), int)
        or not 0 < metadata["total_bytes"] < before[1]
    ):
        raise ValueError("managed Cheat Engine installation manifest is missing or invalid")
    # Validate the immutable executable recorded when this managed tree was
    # promoted. Directory-selection preference may improve in a later plugin
    # version, but that must not silently reinterpret or invalidate an older
    # exact-SHA installation before an explicit reinstall promotes a new one.
    inspected = inspect_ce_selection(str(resolved / metadata["executable"]))
    if metadata.get("executable") != Path(inspected.executable).name or metadata.get("executable_sha256") != inspected.sha256:
        raise ValueError("managed Cheat Engine installation identity changed")
    if _validate_install_tree(resolved) != before:
        raise ValueError("managed Cheat Engine installation changed during validation")
    return inspected


def _promote_installation(
    stage: Path,
    ce_root: Path,
    release: ManagedCERelease,
    legacy_tool: ProtonTool | None = None,
    replace_valid: bool = False,
    provenance: dict[str, object] | None = None,
) -> ImportedCE:
    file_count, total_bytes = _validate_install_tree(stage, allow_managed_manifest=False)
    inspected = inspect_ce_selection(str(stage))
    if _validate_install_tree(stage, allow_managed_manifest=False) != (file_count, total_bytes):
        raise ValueError("managed Cheat Engine installation changed during validation")
    # The executable states its own version, which is the only source that
    # stays right across a manifest whose label was written by hand. The
    # manifest's is a review-time label and remains the fallback.
    version = read_pe_version(Path(inspected.executable)) or release.visible_version
    if _unsafe_text(version, 128):
        version = release.visible_version
    atomic_write_json(stage / ".ce-decky-managed-install.json", {
        "schema": 3, "artifact_sha256": release.sha256, "visible_version": version,
        "executable": Path(inspected.executable).name, "executable_sha256": inspected.sha256,
        "materialization": "native-ce77-extraction",
        "file_count": file_count + 1, "total_bytes": total_bytes,
        "provenance": provenance or _provenance(release, source="reviewed-url"),
    })
    destination_parent = ce_root / "installations"
    _ensure_managed_directory(destination_parent, ce_root, create=True)
    destination = destination_parent / release.sha256
    backup: Path | None = None
    if destination.exists() or destination.is_symlink():
        try:
            existing = validate_managed_installation(destination, ce_root, release)
        except ValueError:
            existing = None
        if existing is not None and not replace_valid:
            return existing
        backup = destination_parent / f".{release.sha256}.previous-{uuid.uuid4().hex}"
        os.replace(destination, backup)
        fsync_directory(destination_parent)
    try:
        os.replace(stage, destination)
        fsync_directory(destination_parent)
        promoted = validate_managed_installation(destination, ce_root, release)
    except BaseException:
        failed_promotion: Path | None = None
        if backup is not None and (backup.exists() or backup.is_symlink()):
            # Preserve the previously validated installation before doing any
            # fallible recursive cleanup.  Moving the failed promotion aside is
            # same-filesystem/atomic; restoring the backup then makes a failed
            # force-reinstall non-destructive even if cleanup of the rejected
            # tree itself later fails.
            if destination.exists() or destination.is_symlink():
                failed_promotion = destination_parent / f".{release.sha256}.failed-{uuid.uuid4().hex}"
                os.replace(destination, failed_promotion)
                fsync_directory(destination_parent)
            os.replace(backup, destination)
            fsync_directory(destination_parent)
            if failed_promotion is not None:
                try:
                    _discard_owned_path(failed_promotion)
                    fsync_directory(destination_parent)
                except OSError:
                    # Restoration is the safety boundary.  A hidden rejected
                    # tree can be reconciled later; never trade the known-good
                    # installation for best-effort cleanup.
                    pass
        elif destination.exists() or destination.is_symlink():
            _discard_owned_path(destination)
            fsync_directory(destination_parent)
        raise
    if backup is not None:
        try:
            _discard_owned_path(backup)
            fsync_directory(destination_parent)
        except OSError:
            # The replacement is already validated and committed at the canonical
            # path.  Failure to remove our hidden previous tree is a recoverable
            # storage leak, not a reason to report a failed reinstall after commit.
            pass
    return promoted


#: Provenance kept beside a cached installer, so reuse cannot lose its origin.
INSTALLER_SOURCE_NAME = ".ce-decky-installer-source.json"
PROVENANCE_SCHEMA = 1
PROVENANCE_KEYS = frozenset({
    "schema", "source", "verified_by", "artifact_sha256", "artifact_bytes",
    "origin_host", "helper_host", "helper_format", "reviewed_at",
    "reviewed_subject", "signature_subject", "signature_common_name",
    "signature_key_sha256", "signature_digest", "signature_note",
})


def _provenance(
    release: ManagedCERelease,
    *,
    source: str,
    resolved: ResolvedInstaller | None = None,
    signature: SignatureFacts | None = None,
    signature_note: str | None = None,
) -> dict[str, object]:
    """How an artifact was obtained, bounded for both the UI and disk metadata.

    No URL is recorded. The host answers where the bytes came from; the full
    link is a live download route nothing downstream needs, and putting it on a
    diagnostics screen would only invite fetching it by hand.
    """
    return {
        "schema": PROVENANCE_SCHEMA,
        "source": source,
        # There is one install authority and it does not vary by route. Kept in
        # the record so a tree promoted by this build still says what it was.
        "verified_by": "reviewed SHA-256",
        "artifact_sha256": release.sha256,
        "artifact_bytes": release.size,
        "origin_host": (
            resolved.host if resolved is not None
            else (urlsplit(release.artifact_url).hostname or "")
        ),
        "helper_host": resolved.stub_host if resolved is not None else None,
        "helper_format": resolved.setup_id if resolved is not None else None,
        "reviewed_at": release.reviewed_at,
        "reviewed_subject": release.reviewed_authenticode_subject,
        "signature_subject": signature.subject if signature is not None else None,
        "signature_common_name": signature.common_name if signature is not None else None,
        "signature_key_sha256": signature.key_sha256 if signature is not None else None,
        "signature_digest": signature.digest_algorithm if signature is not None else None,
        "signature_note": signature_note,
    }


def _valid_provenance(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != PROVENANCE_KEYS:
        return False
    if value.get("schema") != PROVENANCE_SCHEMA:
        return False
    for key, item in value.items():
        if key in {"schema", "artifact_bytes"}:
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                return False
        elif item is not None and (not isinstance(item, str) or _unsafe_text(item, 512)):
            return False
    return isinstance(value.get("source"), str) and isinstance(value.get("verified_by"), str)


def read_managed_provenance(root: Path) -> dict[str, object] | None:
    """The recorded provenance of a promoted managed installation.

    An installation promoted before provenance was recorded has none, and says
    so, rather than being described with a story this plugin cannot support.
    """
    metadata = load_json(root / ".ce-decky-managed-install.json", None, max_bytes=64 * 1024)
    if not isinstance(metadata, dict):
        return None
    provenance = metadata.get("provenance")
    return dict(provenance) if _valid_provenance(provenance) else None


def _verify_installer(path: Path, release: ManagedCERelease) -> None:
    data = read_regular_bytes(path, max_bytes=release.max_size)
    assert data is not None
    if len(data) != release.size or sha256(data).hexdigest() != release.sha256:
        raise ValueError("managed CE installer does not match the reviewed size and SHA-256")
    if len(data) < 64 or data[:2] != b"MZ":
        raise ValueError("managed CE installer is not a PE/MZ executable")


def _managed_setup_error(exc: Exception) -> str:
    """Return a bounded, actionable setup status without exposing transport internals."""
    detail = _safe_status_text(str(exc), 512)
    suffix = " The existing managed installation was kept unchanged."
    if isinstance(exc, RediscoveryError):
        # A device that is simply offline fails here too, and blaming the
        # website would send the user to check something that is fine. The
        # transport cause is answered by the network branches below.
        cause = exc.__cause__
        if isinstance(cause, (NetworkError, ResponseTooLarge)):
            # Say which route failed. The reviewed link already failed above,
            # so repeating its name would send troubleshooting to the wrong
            # host entirely.
            return _managed_setup_error(cause).replace(
                "Reviewed Cheat Engine download", "Current Cheat Engine download",
            ).replace(
                "reviewed Cheat Engine download host", "current Cheat Engine download host",
            ).replace(
                "reviewed Cheat Engine artifact", "current Cheat Engine artifact",
            )
        return (
            "The reviewed Cheat Engine download link no longer works, and the "
            "current one could not be read from cheatengine.org." + suffix
        )
    if "is not the reviewed release and its signature could not be trusted" in detail:
        return (
            "The reviewed Cheat Engine download link no longer works, and the "
            "replacement found on cheatengine.org could not be proven to come "
            "from Cheat Engine's own publisher, so it was discarded." + suffix
        )
    if isinstance(exc, PublisherReleaseUnsupported):
        return (
            "The Cheat Engine download link now serves a different release than "
            "the one this version of CE Decky can install. It came from Cheat "
            "Engine's own publisher, so only the packaging is the problem. Update "
            "CE Decky, or import a Cheat Engine installation." + suffix
        )
    # The extractor is the only thing on this path that reports the installer's
    # own structure. `NetworkError` is a `RuntimeError` too, so it is excluded
    # here rather than left to match a transport message by coincidence.
    if isinstance(exc, RuntimeError) and not isinstance(exc, NetworkError) and any(
        token in detail for token in ("Inno", "loader", "setup data", "CRC", "LZMA", "PE ")
    ):
        return (
            "The Cheat Engine installer that was downloaded is packaged in a "
            "format this version of CE Decky cannot read, so nothing was "
            "extracted. Import a Cheat Engine installation instead." + suffix
        )
    if "does not match the reviewed size and SHA-256" in detail:
        return "Downloaded Cheat Engine artifact did not match the reviewed size and SHA-256 and was discarded." + suffix
    if "is not a PE/MZ executable" in detail:
        return "Downloaded Cheat Engine artifact is not the expected Windows installer and was discarded." + suffix
    if isinstance(exc, DownloadDestinationError):
        # A `NetworkError` subclass, so it would otherwise be answered with
        # "check the connection" for a disk that is full or unwritable.
        if "already exists" in detail:
            return (
                "A leftover file is in the way of the Cheat Engine download in "
                "CE Decky's own storage. Try again; if it keeps happening, use "
                "Advanced to delete CE Decky's staging data." + suffix
            )
        return (
            "CE Decky could not write the Cheat Engine download to its own "
            "storage. Check the free space on this device and try again." + suffix
        )
    if isinstance(exc, ResponseTooLarge) or "exceeds the byte limit" in detail:
        return "Reviewed Cheat Engine download exceeded its safe size limit and was discarded." + suffix
    if isinstance(exc, NetworkError):
        status = re.search(r"\bHTTP ([1-5][0-9]{2})\b", detail)
        if status and status.group(1) in {"404", "410"}:
            return f"Reviewed Cheat Engine download is unavailable (HTTP {status.group(1)}). Try again later." + suffix
        if status and status.group(1) in {"401", "403"}:
            return f"Reviewed Cheat Engine download was refused (HTTP {status.group(1)}). Try again later." + suffix
        if status and status.group(1) == "429":
            return "Reviewed Cheat Engine download is rate limited (HTTP 429). Try again later." + suffix
        if status and status.group(1).startswith("5"):
            return f"Reviewed Cheat Engine download service is temporarily unavailable (HTTP {status.group(1)}). Try again later." + suffix
        if "resolution" in detail:
            return "Could not resolve the reviewed Cheat Engine download host. Check the network connection and try again." + suffix
        if "redirect" in detail:
            return "Reviewed Cheat Engine download redirected to an unsafe or invalid location and was refused." + suffix
        return "Could not download the reviewed Cheat Engine artifact because the network or TLS connection failed. Check the connection and try again." + suffix
    return detail


def _verify_proton_tool(tool: ProtonTool) -> None:
    try:
        root = Path(tool.path).resolve(strict=True)
        script_path = root / "proton"
        script = read_regular_bytes(script_path, max_bytes=16 * 1024 * 1024)
        assert script is not None
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("selected Proton tool is unavailable or unsafe") from exc
    script_sha = sha256(script).hexdigest()
    expected_id = sha256((str(root) + "\0" + script_sha).encode("utf-8")).hexdigest()
    if (
        str(root) != tool.path or script_sha != tool.proton_sha256 or expected_id != tool.tool_id
        or not os.access(script_path, os.X_OK)
    ):
        raise ValueError("selected Proton identity changed before installer execution")


def _validate_install_tree(root: Path, *, allow_managed_manifest: bool = True) -> tuple[int, int]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("managed CE installation root is unsafe")
    count, total, entries = 0, 0, 0
    seen: dict[str, str] = {}
    for path in root.rglob("*"):
        entries += 1
        if entries > MAX_INSTALL_TREE_ENTRIES:
            raise ValueError("managed CE installation exceeds the tree entry bound")
        rel = path.relative_to(root)
        key = _validate_windows_relative_path(rel, allow_reserved=False)
        if key == ".ce-decky-managed-install.json" and not allow_managed_manifest:
            raise ValueError("managed CE installer output collides with CE Decky metadata")
        previous = seen.get(key)
        if previous is not None and previous != rel.as_posix():
            raise ValueError(f"managed CE installation contains Windows-colliding paths: {previous} and {rel.as_posix()}")
        seen[key] = rel.as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("managed CE installation contains a symlink")
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("managed CE installation contains a non-regular file")
        count += 1
        total += info.st_size
        if count > MAX_INSTALL_FILES or total > MAX_INSTALL_BYTES:
            raise ValueError("managed CE installation exceeds configured bounds")
    if count == 0:
        raise ValueError("managed CE installation is empty")
    return count, total


async def _drain_process_output(process: asyncio.subprocess.Process, retained: bytearray) -> None:
    """Keep the merged installer log bounded without ever blocking process exit."""
    assert process.stdout is not None
    while True:
        chunk = await process.stdout.read(64 * 1024)
        if not chunk:
            return
        retained.extend(chunk)
        if len(retained) > MAX_INSTALL_LOG_BYTES:
            del retained[:-MAX_INSTALL_LOG_BYTES]


def _ensure_managed_directory(path: Path, boundary: Path, *, create: bool) -> None:
    boundary = boundary.resolve(strict=True)
    absolute = path.absolute()
    try:
        parts = absolute.relative_to(boundary).parts
    except ValueError as exc:
        raise ValueError("managed CE installer path escaped its owned root") from exc
    current = boundary
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("managed CE installer path contains a symlink")
        if current.exists():
            if not current.is_dir():
                raise ValueError("managed CE installer path contains a non-directory")
        elif create:
            current.mkdir(exist_ok=False)
        else:
            raise ValueError("managed CE installer directory is missing")
        if current.resolve(strict=True) != current.absolute():
            raise ValueError("managed CE installer directory resolves through a symlink")


def _reset_owned_directory(path: Path, boundary: Path) -> None:
    _ensure_managed_directory(path.parent, boundary, create=True)
    if path.is_symlink():
        raise ValueError("managed CE installer prefix must not be a symlink")
    if path.exists():
        if not path.is_dir() or path.resolve(strict=True) != path.absolute():
            raise ValueError("managed CE installer prefix is unsafe")
        shutil.rmtree(path)
        fsync_directory(path.parent)
    path.mkdir(exist_ok=False)
    fsync_directory(path.parent)


def _discard_owned_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def _cleanup_managed_operation_work(work: Path, temp_root: Path) -> None:
    if not work.exists() and not work.is_symlink():
        return
    _ensure_managed_directory(work.parent, temp_root, create=False)
    _discard_owned_path(work)
    fsync_directory(work.parent)


def _bounded_directories(root: Path, limit: int) -> list[Path]:
    result: list[Path] = []
    try:
        iterator = os.scandir(root)
    except OSError:
        return result
    with iterator:
        for entry in iterator:
            if len(result) >= limit:
                break
            try:
                if entry.is_dir(follow_symlinks=False):
                    result.append(Path(entry.path))
            except OSError:
                continue
    return result


def _unsafe_text(value: str, max_bytes: int) -> bool:
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return len(encoded) > max_bytes or any(
        unicodedata.category(ch) in {"Cc", "Cf"} or unicodedata.bidirectional(ch) in _BIDI_CONTROLS
        for ch in value
    )


def _safe_status_text(value: str, max_chars: int) -> str:
    cleaned = "".join(
        ch if unicodedata.category(ch) not in {"Cc", "Cf"} and unicodedata.bidirectional(ch) not in _BIDI_CONTROLS else " "
        for ch in value
    )
    return cleaned[-max_chars:]


def _steam_root_for_tool(tool_root: Path, user_home: Path) -> Path:
    resolved_tool = tool_root.resolve(strict=True)
    existing_roots: list[Path] = []
    for root in (user_home / ".local/share/Steam", user_home / ".steam/steam", user_home / ".steam/root"):
        try:
            resolved_root = root.resolve(strict=True)
            if resolved_root not in existing_roots:
                existing_roots.append(resolved_root)
            resolved_tool.relative_to(resolved_root)
            return resolved_root
        except (OSError, RuntimeError, ValueError):
            continue
    if existing_roots:
        return existing_roots[0]
    raise ValueError("Steam client root is unavailable for the selected Proton tool")
