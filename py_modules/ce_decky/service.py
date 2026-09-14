from __future__ import annotations

from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path
import asyncio
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import unicodedata
import uuid

from .atomic import DurabilityUnknownError, durable_unlink, fsync_directory
from . import __version__
from . import poll_counters
from .activity_log import StateTransitionLog, log_activity, log_failure
from .archive_import import sevenzip_opens_rar
from .ce_import import inspect_ce_selection
from .pe_version import read_pe_version
from .ce_archive_import import (
    archive_digest,
    extract_ce_archive,
    inspect_ce_archive,
    promote_imported_installation,
    snapshot_ce_archive,
    validate_imported_installation,
)
from .ce_launch import (
    CELaunchSupervisor,
    match_observed_proton,
    observe_game_container,
    observe_game_executable_path,
    observe_game_executable_paths,
    observe_running_app_ids,
)
from .ce_runtime import (
    collect_stale_runtimes,
    materialize_private_runtime,
    validate_runtime_identity_manifest,
)
from .config import Config, ConfigStore
from .ct_inspector import TableBlobUnavailable, TableControl, inspect_table
from .table_code import list_table_code as read_code_index, read_table_code as read_code_section
from .game_files import game_executables
from .steam_library import local_library
from .game_identity import Candidate, aliases as game_aliases, decide_match, query_plan
from .managed_ce import discover_steam_library_roots, ManagedCEManager, discover_proton_tools, read_managed_provenance, validate_managed_installation
from .network_tls import build_verified_ssl_context
from .network import NetworkClient
from .operations import drained_to_thread
from .catalog import CatalogService
from .artifact_resolutions import ArtifactResolutions
from .table_compatibility import TableCompatibility, compatibility_state, steam_build_id
from .acquisition import AcquisitionManager
from .paths import PluginPaths
from .profiles import ConfiguredValue, ProfileStore, StartupPreference, is_configurable_value, _optional_process
from .providers import (
    DEFAULT_PROVIDER_DEFINITIONS,
    ProviderDiagnosticsStore,
    normalized_provider_id,
    searchable_providers,
    selectable_providers,
)
from .provider_sources import ProviderSourceSelection
from .session_protocol import MAX_STARTUP_ACTIONS, RuntimeCommand, SessionStore, effective_startup_plan, wine_z_path
from .table_blocklist import CAUSE_REFUSED, CAUSE_UNUSABLE, MAX_REASON_BYTES as MAX_BLOCKED_REASON_BYTES, BlockedTableError, TableBlocklist, is_compatibility_failure
from . import frontend_journal, journal_records
from .support_bundle import create_support_bundle as write_support_bundle, normalize_frontend_log
from .table_store import TableStore
from .text import utf8_len

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_PROCESS_RE = re.compile(r"^[^\\/:*?\"<>|\x00-\x1f]{1,255}\.exe$", re.IGNORECASE)
_ALLOWED_RUNTIME_COMMAND_KEYS = frozenset({"generation", "kind", "record_id", "value", "target_pid"})
RUNTIME_HEARTBEAT_TTL_SECONDS = 3.0
RUNTIME_STATUS_MAX_FUTURE_SKEW_SECONDS = 5.0


def _clamped_reason(value: object) -> str:
    """Bound the recorded reason instead of losing the record over its length.

    The reason names the cheat that was refused, and a table may give a cheat a
    very long description. The record is what stops the same file being found
    and downloaded again, so a reason that does not fit is trimmed rather than
    allowed to throw the whole thing away. Everything else about the text stays
    strictly validated by the store.
    """
    if not isinstance(value, str):
        raise ValueError("blocked table reason must be a string")
    text = value.strip()
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_BLOCKED_REASON_BYTES:
        return text
    ellipsis = "\u2026"
    budget = MAX_BLOCKED_REASON_BYTES - len(ellipsis.encode("utf-8"))
    return encoded[:budget].decode("utf-8", errors="ignore").rstrip() + ellipsis


def _provider_row_keys(table: object) -> tuple[str, ...]:
    """`provider:artifact_id` for every provider row a stored table came from.

    A search result carries a provider and an artifact ID always, and an
    advertised content digest only sometimes, so this is what lets a durable
    mark grey the row it was recorded from. Provider-described data throughout:
    an origin that cannot be read is skipped rather than failing the block that
    is being recorded for a table already known not to work.
    """
    if not isinstance(table, dict):
        return ()
    origins = table.get("origins")
    if not isinstance(origins, list):
        return ()
    keys: list[str] = []
    for origin in origins:
        if not isinstance(origin, dict):
            continue
        provider = origin.get("provider")
        artifact_id = origin.get("artifact_id")
        if not isinstance(provider, str) or not isinstance(artifact_id, str):
            continue
        provider, artifact_id = provider.strip(), artifact_id.strip()
        if not provider or not artifact_id:
            continue
        key = f"{provider}:{artifact_id}"
        if key not in keys:
            keys.append(key)
    return tuple(keys)


def _advertised_release(table: object) -> str | None:
    """The release the source stated for a stored table's newest download.

    Recorded beside a not-working mark because the mark outlives the file: a
    user reading that list months later has one line to tell this revision from
    the one beside it, and a post carries every revision of a table under one
    filename and one title. The newest origin, which is the download the rest of
    a row describes; never searched for across all of them, because a version
    belonging to an earlier download is a different table's release.

    Provider-described data, so an origin that cannot be read is skipped rather
    than failing a block that is being recorded for a table already known not to
    work. The `.CT` file's own `CheatEngineTableVersion` is deliberately not a
    fallback here: it is the version of Cheat Engine's table format, not of the
    table, and every table on a device reads 45 or 46 whatever its game.
    """
    if not isinstance(table, dict):
        return None
    origins = table.get("origins")
    if not isinstance(origins, list) or not origins:
        return None
    # The newest origin and no other. This walked back through all of them,
    # which is the thing the paragraph above says it must not do: a table
    # downloaded once from a row advertising 1.6 and re-encountered later on a
    # row advertising nothing would have been recorded as 1.6, while Manage and
    # Search, which read only the newest, correctly claimed no release. Two
    # screens disagreeing about the one field that tells two revisions apart is
    # worse than neither of them having it.
    newest = origins[-1]
    if not isinstance(newest, dict):
        return None
    version = newest.get("version")
    return version.strip() if isinstance(version, str) and version.strip() else None


# What a screen may call its own search by. It names nothing and authorizes
# nothing: it exists so that a caller reading progress can be told about its own
# search and about no other, and it is bounded and constrained here because it
# arrives from the frontend like any other argument.
_SEARCH_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _search_token(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _SEARCH_TOKEN_RE.fullmatch(value):
        raise ValueError("search progress token is malformed")
    return value


def _runtime_protocol_reason(exc: ValueError) -> str:
    """Describe unreadable mutable control/status state as one repairable reason."""
    return f"runtime protocol state is unreadable: {str(exc)[:448]}"


def _disconnected_bridge_reason(runtime: dict[str, object]) -> str:
    """Which of the four things a bridge that is not connected actually is.

    "Not connected with a fresh heartbeat" covered a session that was never
    prepared, one whose Cheat Engine has been proven gone, one whose state
    cannot be read, and one that is alive and simply busy running the table's
    own script. Only the last is worth trying again, and the reader could not
    tell which they had.
    """
    if runtime.get("prepared") is None:
        reason = runtime.get("session_stale_reason")
        return str(reason) if isinstance(reason, str) and reason else "no prepared session exists for this game"
    if runtime.get("terminal_reason") == "owned_bridge_process_exited":
        return "the Cheat Engine that held this session has exited; start it again for this game"
    if not bool(runtime.get("session_current")):
        reason = runtime.get("session_stale_reason")
        return str(reason) if isinstance(reason, str) and reason else "this game's prepared session is no longer current"
    if runtime.get("status") is None:
        return "the resident bridge has not reported its state for this session yet"
    if bool(runtime.get("status_clock_skew")):
        return "the resident bridge's last heartbeat is dated in the future, so its age cannot be judged"
    age = runtime.get("status_age_ms")
    if isinstance(age, int) and not isinstance(age, bool):
        return (
            f"the resident bridge has not written a heartbeat for {age // 1000}s; "
            "it stops writing one while Cheat Engine runs the table's own script"
        )
    return "resident bridge is not connected with a fresh heartbeat"


class PluginService:
    def __init__(self, paths: PluginPaths, logger) -> None:
        self.paths = paths
        self.logger = logger
        self.config_store = ConfigStore(paths.config_path)
        self.artifact_resolutions = ArtifactResolutions(paths.state_root / "artifact_resolutions.json")
        self.table_blocklist = TableBlocklist(paths.state_root / "blocked_tables.json")
        self.table_store = TableStore(
            paths.tables_root,
            assert_importable=self._assert_table_importable,
            record_unusable=self._record_unusable_table,
        )
        self.profile_store = ProfileStore(paths.state_root / "profiles.json")
        self.session_store = SessionStore(paths.state_root, paths.user_home)
        self.provider_diagnostics = ProviderDiagnosticsStore(paths.cache_root / "providers.json")
        # The user's own choice of sources. Durable state rather than cache,
        # because deleting the cache is offered as a safe repair and a switched
        # off source coming back on by itself is not a repair.
        self.provider_sources = ProviderSourceSelection(paths.state_root / "provider_sources.json")
        # Logged once per change rather than once per read: the catalog asks for
        # this several times per search, and an unreadable file would otherwise
        # fill the log with the same line instead of describing the fault once.
        self._provider_sources_reason: str | None = None
        # The last RAR capability logged. The archive adapter owns the cache by
        # executable identity; this pair only avoids repeating an unchanged log.
        self._rar_probe: tuple[str | None, bool | None] = (None, None)
        tls_context, _ = build_verified_ssl_context()
        self.provider_catalog = CatalogService(
            NetworkClient(tls_context), paths.cache_root / "provider-results.json", self.provider_diagnostics,
            logger=logger, disabled_providers=self._disabled_provider_ids,
            rar_openable=self._sevenzip_opens_rar,
        )
        self.acquisitions = AcquisitionManager(
            self.provider_catalog,
            self.provider_catalog.network,
            self.table_store,
            paths.temp_root,
            paths.user_home / "Downloads",
            paths.user_home,
            self._find_7zip,
            logger=logger,
        )
        self.acquisitions.record_resolution = self.artifact_resolutions.record
        self.acquisitions.record_missing = self.block_missing_artifact
        self.managed_ce = ManagedCEManager(
            paths.user_home, paths.ce_root, paths.temp_root,
            self.provider_catalog.network, logger,
        )
        self.ce_launch = CELaunchSupervisor(paths.user_home, paths.ce_root, paths.state_root, logger)
        self.bridge_source = Path(__file__).with_name("ce_decky_bridge.lua")
        # The panel polls runtime status and re-inspects the active table
        # continuously. Both are the evidence a bug report needs and neither can
        # be logged per call, so each is logged only when what it says changes.
        self._runtime_journal = StateTransitionLog(logger, "runtime.state_changed")
        self._table_journal = StateTransitionLog(logger, "table.inspected")
        self._started_monotonic = time.monotonic()
        self._mutation_lock = threading.RLock()
        self.table_compatibility = TableCompatibility(paths.state_root / "table_compatibility.json")
        self._compatibility_pending = {}
        self._startup_compatibility_pending = {}
        self._launch_reservations: dict[int, tuple[str, str, str]] = {}
        # Managed setup spans asynchronous download/native extraction plus a final
        # synchronous identity commit.  Keep a service-level reservation across
        # that entire lifecycle so blocking RPCs cannot materialize or replace
        # CE identity while the managed tree is being promoted underneath them.
        self._managed_ce_reservation: str | None = None
        # Decky may briefly run overlapping frontend observers for one completed
        # setup operation. Keep one in-memory, exact-ID completion receipt until
        # the next setup begins so a losing observer can reconcile without
        # repeating the identity mutation or surfacing an RPC exception.
        self._managed_ce_completion: tuple[str, dict[str, object]] | None = None

    def initialize(self) -> None:
        self.paths.ensure()
        with self._mutation_lock:
            self.table_store.reconcile_orphan_blobs(self.logger)
        if not self.paths.config_path.exists():
            self.config_store.save(self.config_store.load())
        self._label_legacy_ce_registration()

    def start_background_work(self) -> None:
        """Start what the plugin does for itself while nobody is using it.

        Separate from `initialize()` because it needs a running event loop and
        that one is also called from tools that have none. Called once per load,
        after recovery, so that a device in ordinary use keeps its provider
        listing current instead of only refreshing it while somebody searches.
        """
        self.provider_catalog.start_background_index()

    def _label_legacy_ce_registration(self) -> None:
        """Give a registration written before versions were read its label, once.

        Only a `Config` from an older build can be missing this, and nothing at
        runtime can produce another: every path that registers a Cheat Engine
        reads the version with it. So the whole question is settled at load,
        which is an explicit mutation boundary, and no read path has to carry a
        write in order to answer it.

        It used to happen inside `get_status()`, which the panel polls and which
        several read-only developer helpers call for identity. Those helpers
        promise to write nothing, and on a legacy registration they did.

        A failure here is a missing label and never a reason to fail load.
        """
        try:
            config = self.config_store.load()
            if not config.imported_ce_executable or config.imported_ce_version is not None:
                return
            try:
                self._validated_ce_import()
            except ValueError:
                # An unusable registration gets no label and no write. The
                # status call reports why it is unusable, as it already did.
                return
            version = read_pe_version(Path(config.imported_ce_executable))
            if version is not None:
                self._backfill_ce_version(config, version)
        except Exception as exc:  # noqa: BLE001 - a label must never block backend load
            log_failure(self.logger, "ce.version_label_failed", exc, expected=True)

    def _backfill_ce_version(self, observed: Config, version: str) -> None:
        """Record a missing version label for an exact CE registration.

        This is the only write the label pass performs, and it must not behave
        like one. Saving the whole `Config` that pass loaded would race
        every real identity mutation: an import, archive import, forget or
        managed registration committed under `_mutation_lock` while this worker
        was reading the PE would be overwritten by the stale snapshot this call
        started from, resurrecting the previous Cheat Engine. Re-read under the
        same lock and commit the label only while the exact identity whose
        version was read is still the registered one.
        """
        with self._mutation_lock:
            try:
                current = self.config_store.load()
            except ValueError:
                return
            identity = (current.imported_ce_executable, current.imported_ce_root, current.imported_ce_sha256)
            if identity != (observed.imported_ce_executable, observed.imported_ce_root, observed.imported_ce_sha256):
                return
            if current.imported_ce_version is not None:
                return
            current.imported_ce_version = version
            self.config_store.save(current)

    def _managed_ce_identity(self, config: Config) -> tuple[bool, dict[str, object] | None]:
        """Whether the registered Cheat Engine is one this plugin installed, and how.

        Managed installations live in one content-addressed directory, so the
        registered root answers this without comparing against whatever the
        packaged manifest currently pins - which stops being the right test the
        moment a rediscovered artifact is a newer release than the reviewed one.
        """
        if not config.imported_ce_root:
            return False, None
        try:
            root = Path(config.imported_ce_root)
            if root.parent != self.paths.ce_root / "installations":
                return False, None
            return True, read_managed_provenance(root)
        except (OSError, ValueError):
            return False, None

    def get_poll_counters(self) -> dict[str, object]:
        """Read the repeating-path counters without adding to any of them.

        This exists for the tracked cost harness, not for the panel. What it
        answers is which of the backend's repeating paths was alive across a
        measured window and what each of them spent there, which is the one
        thing a figure for the whole process cannot say.
        """
        return poll_counters.snapshot()

    def get_status(self, current_app_id: int | None = None) -> dict[str, object]:
        if current_app_id is not None and (type(current_app_id) is not int or not 0 < current_app_id <= 0xFFFFFFFF):
            raise ValueError("invalid current AppID")
        try:
            config = self.config_store.load()
            config_state_reason: str | None = None
        except ValueError as exc:
            config = Config()
            config_state_reason = str(exc)[:512]
        sevenzip = self._find_7zip()
        ce_valid = False
        ce_reason: str | None = config_state_reason
        if config_state_reason is None and config.imported_ce_executable:
            try:
                self._validated_ce_import()
                ce_valid = True
            except ValueError as exc:
                ce_reason = str(exc)
        # `get_status()` writes nothing. A registration from before this plugin
        # read versions is labelled once at load, in `_label_legacy_ce_registration`,
        # because the panel polls this call and several read-only developer
        # helpers ask it for identity.
        ce_managed, ce_provenance = self._managed_ce_identity(config)
        try:
            tables, table_catalog_errors = self.table_store.list_tables_with_errors()
            table_state_reason: str | None = None
        except ValueError as exc:
            tables = []
            table_catalog_errors = []
            table_state_reason = str(exc)[:512]
        try:
            profiles = [profile.as_dict() for profile in self.profile_store.list_profiles()]
            profile_state_reason: str | None = None
        except (OSError, ValueError) as exc:
            profiles = []
            profile_state_reason = str(exc)[:512]
        return {
            "version": __version__,
            "user_home": str(self.paths.user_home),
            "managed_root": str(self.paths.managed_root),
            "settings_dir": str(self.paths.settings_dir),
            # An authenticated local caller can use the path Decky supplied to
            # this exact backend as installation authority. In particular, the
            # target installer must not have to read another process's environ,
            # which SteamOS may forbid even for the same unprivileged user.
            "plugin_dir": str(self.paths.plugin_dir),
            "log_dir": str(self.paths.log_dir),
            "log_file": os.environ.get("DECKY_PLUGIN_LOG", ""),
            "sevenzip": sevenzip,
            "ce": {
                "configured": bool(config.imported_ce_executable),
                "valid": ce_valid,
                "executable": config.imported_ce_executable,
                "sha256": config.imported_ce_sha256,
                "version": config.imported_ce_version,
                "reason": ce_reason,
                "managed": ce_managed,
                "provenance": ce_provenance,
            },
            "tables": tables,
            "artifact_resolutions": self.artifact_resolutions.snapshot(),
            "table_compatibility": self._compatibility_snapshot(current_app_id),
            "profiles": profiles,
            "config_state_reason": config_state_reason,
            "table_state_reason": table_state_reason,
            "table_catalog_errors": table_catalog_errors,
            "profile_state_reason": profile_state_reason,
            "features": {
                "local_ce_import": True,
                "local_ct_import": True,
                "local_archive_import": True,
                "table_inspector": True,
                "game_profiles": True,
                "session_protocol": True,
                "private_ce_runtime_materializer": True,
                "launch_plan_core": True,
                # These flags mean shipped implementation, not proof that the
                # behaviour was observed on a device.
                "launch_integration": True,
                "lua_bridge": True,
                "provider_offline_core": True,
                "provider_network": True,
                "managed_ce_install": True,
                "controlled_cef": False,
                "hot_table_switch": False,
            },
        }

    def self_test(self) -> dict[str, object]:
        checks: list[dict[str, object]] = []
        self.paths.ensure()
        checks.append(self._writable_check("settings", self.paths.settings_dir))
        checks.append(self._writable_check("managed_root", self.paths.managed_root))
        checks.append(self._same_filesystem_check())
        checks.append(self._bridge_asset_check())
        checks.append(self._config_state_check())
        checks.append(self._table_state_check())
        checks.append(self._profiles_state_check())
        sevenzip = self._find_7zip()
        checks.append({
            "name": "host_7zip",
            "ok": sevenzip is not None,
            "detail": sevenzip or "7z/7za/7zr not found; .7z and .rar import are unavailable (ZIP and direct .CT remain available)",
            "blocking": False,
        })
        try:
            tls_context, explicit_ca = build_verified_ssl_context()
            root_count = int(tls_context.cert_store_stats().get("x509_ca", 0))
            checks.append({
                "name": "tls_trust_roots",
                "ok": root_count > 0,
                "detail": (
                    f"{root_count} CA roots loaded"
                    + (f" via {explicit_ca}" if explicit_ca else " via interpreter/system defaults")
                    if root_count > 0
                    else "no CA roots loaded; future HTTPS provider access will fail closed"
                ),
                "blocking": False,
            })
        except Exception as exc:
            checks.append({
                "name": "tls_trust_roots",
                "ok": False,
                "detail": f"TLS trust-store probe failed closed: {exc}",
                "blocking": False,
            })
        checks.append(self._panel_journal_check())
        checks.append(self._system_journal_check())
        checks.append(self._managed_space_check())
        checks.append(self._cheat_engine_identity_check())
        ok = all(bool(item["ok"]) for item in checks if bool(item.get("blocking", True)))
        return {"ok": ok, "checks": checks}

    def _panel_journal_check(self) -> dict[str, object]:
        """Whether the panel's record can still be written, and kept private.

        The panel's own ring lives in the renderer, and recovering a wedged
        Quick Access panel destroys it. This file is the copy that survives, so
        a state root that cannot be written is a bug report with no frontend
        evidence in it, discovered at the moment somebody needs one.
        """
        path = frontend_journal.journal_path(self.paths.state_root)
        # Never by appending to the record itself. This file answers one
        # question, what the panel did last before it stopped, and a line the
        # backend wrote while somebody pressed a diagnostic would answer it
        # wrongly. Writability is proved beside it, exactly as the directory
        # checks above prove theirs.
        writable = self._writable_check("panel_journal", self.paths.state_root)
        if not writable["ok"]:
            return {**writable, "blocking": False}
        try:
            mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else frontend_journal.JOURNAL_MODE
        except OSError as exc:
            return {"name": "panel_journal", "ok": False, "detail": str(exc)[:200], "blocking": False}
        if mode != frontend_journal.JOURNAL_MODE:
            return {
                "name": "panel_journal", "ok": False, "blocking": False,
                "detail": f"the panel record is mode {mode:04o} and holds game and table names; it should be 0600",
            }
        return {"name": "panel_journal", "ok": True, "detail": f"writable and {mode:04o}", "blocking": False}

    def _system_journal_check(self) -> dict[str, object]:
        """Whether this plugin's own records can actually be read back.

        Decky keeps one log file per plugin load and deletes the older ones, so
        the journal is what still holds a failure after the install that came
        next. It was collected by a call that inherited the loader bundle's
        library path and failed on every device, and nothing noticed until a
        bundle was opened: the archive said so in a note where the journal
        should have been. This is that call, bounded, so the next time it breaks
        the answer is here rather than in an archive nobody has collected yet.
        """
        if not journal_records.available():
            return {
                "name": "system_journal", "ok": True, "blocking": False,
                "detail": "journalctl is not on this host; the bundle carries the per-load log files only",
            }
        collected = journal_records.collect(since="-1min", max_lines=1)
        if not collected.get("ok"):
            return {
                "name": "system_journal", "ok": False, "blocking": False,
                "detail": str(collected.get("reason", "journalctl could not be read"))[:200],
            }
        return {"name": "system_journal", "ok": True, "detail": "readable", "blocking": False}

    def _managed_space_check(self) -> dict[str, object]:
        """How much room the plugin's own root has left.

        A device with no room left fails in ways that do not name it: a table
        import that cannot stage, a cache write that fails, a Cheat Engine
        install that cannot promote. It is cheap to ask once here and say it in
        the user's terms instead.
        """
        try:
            usage = shutil.disk_usage(self.paths.managed_root)
        except OSError as exc:
            return {"name": "managed_root_space", "ok": False, "detail": str(exc)[:200], "blocking": False}
        free_mb = usage.free // (1024 * 1024)
        return {
            "name": "managed_root_space",
            "ok": usage.free >= MIN_FREE_MANAGED_BYTES,
            "detail": (
                f"{free_mb} MB free"
                if usage.free >= MIN_FREE_MANAGED_BYTES
                else f"{free_mb} MB free; imports, installs and cache writes start failing near here"
            ),
            "blocking": False,
        }

    def _cheat_engine_identity_check(self) -> dict[str, object]:
        """Whether the registered Cheat Engine is still the one that was registered.

        Every runtime decision is keyed by the exact executable SHA, so an
        installation that changed or went away under the plugin turns into a
        refusal at the moment somebody presses Start, with the reason buried in
        a launch path. The same validation the status read performs, asked
        early and said plainly.
        """
        try:
            config = self.config_store.load()
        except ValueError as exc:
            return {"name": "cheat_engine_identity", "ok": False, "detail": str(exc)[:200], "blocking": False}
        if not config.imported_ce_executable:
            return {
                "name": "cheat_engine_identity", "ok": True, "blocking": False,
                "detail": "no Cheat Engine registered yet; Home offers setup",
            }
        try:
            self._validated_ce_import()
        except ValueError as exc:
            return {"name": "cheat_engine_identity", "ok": False, "detail": str(exc)[:200], "blocking": False}
        return {
            "name": "cheat_engine_identity", "ok": True, "blocking": False,
            "detail": f"{str(config.imported_ce_sha256)[:12]} still matches its registration",
        }

    def _config_for_ce_identity_write(self) -> Config:
        """Load the config, or start a clean one when it cannot be read.

        Read paths already degrade an invalid config into a default snapshot
        plus a reason, so Home stays usable and managed setup still looks
        available. Every write path required that same config to load, though,
        so a corrupt one could be detected and never repaired: import, archive
        import and forget all failed on it, and managed install could download,
        verify and atomically promote a Cheat Engine and then fail at
        registration - leaving a completed operation that kept the whole panel
        busy with no controller action able to fix it. The only recovery was
        editing files from a terminal, which the Game Mode contract forbids.

        Every caller of this sets all four Cheat Engine fields itself, so
        replacing an unreadable payload discards nothing it was not about to
        overwrite.
        """
        try:
            return self.config_store.load()
        except ValueError as exc:
            log_activity(
                self.logger, "warning", "config.replaced_unreadable",
                reason=str(exc)[:256],
            )
            return Config()

    def import_ce(self, selection: str) -> dict[str, object]:
        with self._mutation_lock:
            self._assert_no_managed_ce_transition()
            # Re-importing changes the executable identity for every prepared
            # session, so do not strand a currently owned CE without stop
            # controls.
            for profile in self.profile_store.list_profiles():
                self._assert_no_live_owned_launch(profile.app_id)
            imported = inspect_ce_selection(selection)
            executable = Path(imported.executable)
            try:
                executable.relative_to(self.paths.user_home)
            except ValueError as exc:
                raise ValueError(
                    f"stable v1 requires imported Cheat Engine to live under DECKY_USER_HOME ({self.paths.user_home})"
                ) from exc
            root = Path(imported.root)
            if root == self.paths.user_home:
                raise ValueError("Cheat Engine executable must live in a dedicated installation directory, not directly in DECKY_USER_HOME")
            try:
                executable.relative_to(self.paths.managed_root)
            except ValueError:
                pass
            else:
                raise ValueError("select the original Cheat Engine installation, not a CE Decky managed runtime path")
            config = self._config_for_ce_identity_write()
            config.imported_ce_executable = imported.executable
            config.imported_ce_root = imported.root
            config.imported_ce_sha256 = imported.sha256
            config.imported_ce_version = read_pe_version(Path(imported.executable))
            self.config_store.save(config)
            log_activity(
                self.logger, "info", "ce_import.completed",
                executable_sha=imported.sha256[:12], path=imported.executable,
                version=config.imported_ce_version or "unknown",
            )
            return {**imported.as_dict(), "version": config.imported_ce_version}

    def import_ce_archive(self, selection: str) -> dict[str, object]:
        """Register a Cheat Engine installation packed into a .zip archive.

        The archive is inspected before a byte is written: its members are
        validated, the installation root is located inside whatever nesting the
        packing produced, and the tree is checked for the files a Cheat Engine
        installation must have. Extraction goes to a plugin-owned staging
        directory and is promoted only after the executable itself validates.
        """
        with self._mutation_lock:
            self._assert_no_managed_ce_transition()
            for profile in self.profile_store.list_profiles():
                self._assert_no_live_owned_launch(profile.app_id)
            source = Path(selection).expanduser().resolve()
            try:
                source.relative_to(self.paths.user_home)
            except ValueError as exc:
                raise ValueError(
                    f"the archive must live under DECKY_USER_HOME ({self.paths.user_home})"
                ) from exc
            try:
                source.relative_to(self.paths.managed_root)
            except ValueError:
                pass
            else:
                raise ValueError("select your own archive, not a file inside CE Decky's managed directory")
            imported_root = self.paths.ce_root / "imported"
            stage = self.paths.temp_root / f"ce-archive-{uuid.uuid4().hex}"
            stage.parent.mkdir(parents=True, exist_ok=True)
            snapshot = snapshot_ce_archive(source, self.paths.temp_root)
            try:
                # Inspection, provenance and extraction all consume the same
                # immutable plugin-owned bytes. A Downloads file can no longer
                # be replaced between those three trust decisions.
                layout = inspect_ce_archive(snapshot)
                digest = archive_digest(snapshot)
                extract_ce_archive(snapshot, layout, stage)
                installed = promote_imported_installation(stage, imported_root, digest, layout)
            finally:
                snapshot.unlink(missing_ok=True)
                if stage.exists():
                    shutil.rmtree(stage, ignore_errors=True)
            config = self._config_for_ce_identity_write()
            config.imported_ce_executable = installed.executable
            config.imported_ce_root = installed.root
            config.imported_ce_sha256 = installed.sha256
            config.imported_ce_version = read_pe_version(Path(installed.executable))
            self.config_store.save(config)
            log_activity(
                self.logger, "info", "ce_import.archive_completed",
                archive_sha=digest[:12], executable_sha=installed.sha256[:12],
                files=layout.file_count, root=layout.root or "<archive root>",
            )
            return {**installed.as_dict(), "version": config.imported_ce_version,
                    "archive_sha256": digest, "archive_root": layout.root,
                    "file_count": layout.file_count, "total_bytes": layout.total_bytes}

    def clear_ce_import(self) -> None:
        with self._mutation_lock:
            self._assert_no_managed_ce_transition()
            for profile in self.profile_store.list_profiles():
                self._assert_no_live_owned_launch(profile.app_id)
            config = self._config_for_ce_identity_write()
            config.imported_ce_executable = None
            config.imported_ce_root = None
            config.imported_ce_sha256 = None
            config.imported_ce_version = None
            self.config_store.save(config)

    # -- tables the user marked as not working -----------------------------
    #
    # Advisory, durable and keyed by exact content. It refuses a re-import and
    # greys a search result; it never authorizes anything, and a table already
    # selected keeps working until the user changes it.

    def _assert_table_importable(self, digest: str, origins: tuple[str, ...] = ()) -> None:
        """Refuse bytes already recorded as not working, naming the reason.

        A blocklist that cannot be read is not evidence that anything is
        blocked, so an unreadable one lets the import through. Failing closed
        here would turn one corrupt advisory file into "no table can be
        imported", which is a far worse outcome than offering a table the user
        has to reject once more.

        A refusal is also the moment the provider row is known for an entry that
        was written without one, so it is merged in on the way past. Search can
        then recognise the post rather than only these bytes, which is what
        stops the same archive being offered and downloaded to reach this same
        refusal again.
        """
        try:
            self.table_blocklist.assert_importable(digest)
        except BlockedTableError:
            if origins:
                try:
                    if self.table_blocklist.add_origins(digest, origins):
                        log_activity(
                            self.logger, "info", "table_blocklist.origin_merged",
                            table_sha=digest[:12], origins=",".join(origins[:4]),
                        )
                except (OSError, ValueError) as exc:
                    # Bookkeeping on top of a refusal that is already correct.
                    log_activity(
                        self.logger, "warning", "table_blocklist.origin_merge_failed",
                        table_sha=digest[:12], reason=str(exc)[:256],
                    )
            raise
        except (OSError, ValueError) as exc:
            # Both, because both are ways the record is unreadable and the rule
            # is about the record, not about how reading it failed. Every other
            # reader of this file already catches the pair and this one caught
            # only `ValueError`. It is defensive rather than a fixed bug: the
            # bounded reader turns every open and stat failure into a
            # `ValueError` itself, so what is left to reach here is a read that
            # fails mid-descriptor, which no test in this suite can produce
            # honestly. One word, and it cannot cost anything.
            log_activity(
                self.logger, "warning", "table_blocklist.unreadable",
                reason=str(exc)[:256],
            )

    def _game_context(self, app_id: object) -> tuple[int | None, str | None]:
        """The game an entry in the not-working list was tried on.

        A file name is not an answer to "what is this table for": the list is
        read months later, and `winmm-x64.zip` beside a raw provider row said
        nothing at all about which game either of them belonged to. The AppID
        travels with the press that produced the record, and the name is looked
        up here rather than taken from the caller, because the profile store
        already holds it and a display name a screen passed in is one more thing
        that can be stale.

        Nothing here may fail the write it is decorating: a game that cannot be
        read is a record with no game, which is what the list showed before.
        """
        if not isinstance(app_id, int) or isinstance(app_id, bool):
            return None, None
        try:
            profile = self.profile_store.get(app_id)
        except (OSError, ValueError):
            return app_id, None
        return app_id, None if profile is None else profile.name

    def _record_unusable_table(
        self, digest: str, reason: str, filename: str, origins: tuple[str, ...] = (),
        app_id: object = None, cause: str = CAUSE_UNUSABLE, table_version: str | None = None,
    ) -> None:
        """Remember bytes that cannot yield a table on this device.

        Best effort on purpose: the import is already failing for a reason the
        caller will report, and a blocklist write that cannot happen must not
        replace that reason with one about bookkeeping.

        The cause comes from the caller because only the caller knows it. Bytes
        that are not a table and an archive nothing here can open are both
        terminal for these exact bytes, and they are not the same statement to
        a user: one of them they can do something about.
        """
        game_id, game_name = self._game_context(app_id)
        try:
            # Re-entrant: the import route already holds this, the acquisition
            # route does not, and both do a read-modify-write of the same file.
            with self._mutation_lock:
                # These bytes never enter the catalog, so unlike a table that
                # was imported and later refused, the provider row cannot be
                # looked up here: the download route passes it in. Without it
                # the record exists and search cannot recognise the row that
                # produced it, so the row is offered, paid for and refused at
                # import all over again.
                self.table_blocklist.block(
                    sha256=digest, reason=_clamped_reason(reason), filename=filename,
                    app_id=game_id, game_name=game_name, table_version=table_version,
                    origins=origins, cause=cause,
                )
            log_activity(
                self.logger, "info", "table_blocklist.recorded",
                table_sha=digest[:12], cause=cause,
            )
        except (OSError, ValueError) as exc:
            log_activity(
                self.logger, "warning", "table_blocklist.write_failed",
                table_sha=digest[:12], reason=str(exc)[:256],
            )

    def list_blocked_tables(self) -> dict[str, object]:
        try:
            entries = self.table_blocklist.list_blocked()
        except (OSError, ValueError) as exc:
            return {"schema": 1, "reason": str(exc)[:448], "tables": []}
        # Read once for the whole list rather than once per row. `get()` parses
        # the profile store from disk on every call, and the rows that need a
        # name are exactly the ones that ask: a list holding the full 512
        # entries would have parsed that file 512 times to answer one read.
        game_names: dict[int, str] = {}
        if any(entry.game_name is None and entry.app_id is not None for entry in entries):
            try:
                game_names = {
                    profile.app_id: profile.name
                    for profile in self.profile_store.list_profiles()
                    if profile.name
                }
            except (OSError, ValueError):
                # A profile store that cannot be read names no game, which is
                # the state these rows were already in.
                game_names = {}
        # What the read found missing, gathered before anything is written. The
        # repairs used to be made one row at a time, and each one loaded the
        # whole store, saved the whole store and parsed it again on the way to
        # carrying the failure epochs: at the 512 entries this store allows,
        # answering one read meant a four figure number of parses and 512
        # durable replacements of one file, worst on exactly the large
        # part-named store that needs the most repair.
        origins_to_add: dict[str, tuple[str, ...]] = {}
        names_to_add: dict[str, str] = {}
        for entry in entries:
            # A record written before provider rows were tracked knows only the
            # digest, so search could not recognise the very tables this user
            # had already proved broken. The catalog still holds where those
            # bytes came from, so fill it in here rather than making them prove
            # it a second time.
            #
            # And write it back, which this used to leave to "the next time the
            # record is written" - a moment that never came for an entry nobody
            # touched again. The record then still held nothing, and the whole
            # recognition depended on the table's own copy still being on disk:
            # delete the plugin's data, or lose that copy any other way, and a
            # table the user had already proved broken was offered and
            # downloadable again with nothing left to recognise it by.
            if not entry.origins:
                try:
                    recovered = _provider_row_keys(self.table_store.get_table(entry.sha256))
                except (OSError, ValueError):
                    recovered = ()
                if recovered:
                    origins_to_add[entry.key] = recovered
            # The same repair, for the other half of a row's identity. A record
            # written while a download was failing can predate this game having
            # a profile at all: the AppID travelled with the press and the name
            # had nowhere to be read from, so the row named a file and no game
            # in a list that spans every game. The name exists by the time
            # anyone reads the list, so it is filled in here and written back,
            # rather than leaving that row unreadable for the life of the
            # record.
            if entry.game_name is None and entry.app_id is not None:
                name = game_names.get(entry.app_id)
                if name:
                    names_to_add[entry.key] = name

        repaired: dict[str, object] = {}
        if origins_to_add or names_to_add:
            try:
                # One write, held against the same lock every other write on
                # this store is. Without that lock a "Stop using it" landing
                # between this repair's read and its write is lost, and a table
                # the user has just marked is not recorded at all.
                with self._mutation_lock:
                    repaired = dict(self.table_blocklist.repair(
                        names=names_to_add, origins=origins_to_add,
                    ))
            except (OSError, ValueError) as exc:
                # Reading the list must not fail because the repair could not be
                # saved; it is retried on the next read. A write can also raise
                # after its replacement is already visible, which is what an
                # unprovable directory sync means here, so what is on disk is
                # asked rather than assumed: the answer must never describe a
                # wider set than the next read of the same list will.
                log_activity(
                    self.logger, "warning", "table_blocklist.repair_failed",
                    names=len(names_to_add), origins=len(origins_to_add), reason=str(exc)[:256],
                )
                try:
                    with self._mutation_lock:
                        repaired = {item.key: item for item in self.table_blocklist.list_blocked()}
                except (OSError, ValueError):
                    repaired = {}

        # The record rather than the repair that was proposed for it. A table
        # carries more rows than a record retains, so publishing the recovery
        # itself made the first read after a repair describe a wider set than
        # the one on disk, and the same list read again said something else.
        listed = []
        for entry in entries:
            item = (repaired[entry.key] if entry.key in repaired else entry).as_dict()
            # A name the write could not keep is still shown from this read. It
            # was read from the profile store rather than proposed for the
            # record, so showing it describes nothing the next read will not
            # also say; the write is retried then. Rows are the opposite case
            # and are never published ahead of the record: a table carries more
            # of them than a record retains.
            if item.get("game_name") is None and entry.key in names_to_add:
                item["game_name"] = names_to_add[entry.key]
            listed.append(item)
        return {"schema": 1, "reason": None, "tables": listed}

    def block_missing_artifact(
        self, provider: str, artifact_id: str, reason: str,
        app_id: object = None, filename: str | None = None,
    ) -> None:
        """Record a provider row whose file the source says it does not have.

        Best effort on a path that is already failing: the acquisition ends the
        same way whether or not this lands. It is recorded because the row is
        otherwise offered, waited for and paid for again on the next search, and
        it goes into the list the user reads and clears, since to them it is the
        same statement about the same row.
        """
        game_id, game_name = self._game_context(app_id)
        try:
            with self._mutation_lock:
                self.table_blocklist.block_row(
                    origin=f"{provider}:{artifact_id}", reason=reason,
                    # The row's own file name, which is the only thing about it
                    # a user can recognise: the list was showing the provider
                    # key it is stored under, which names neither a game nor a
                    # table.
                    filename=filename, app_id=game_id, game_name=game_name,
                )
            log_activity(
                self.logger, "info", "table_blocklist.recorded",
                table_sha="none", cause="artifact_gone",
            )
        except (OSError, ValueError) as exc:
            log_activity(
                self.logger, "warning", "table_blocklist.write_failed",
                table_sha="none", reason=str(exc)[:256],
            )

    def block_table(self, sha256: str, reason: str, app_id: int | None = None) -> dict[str, object]:
        """Record one exact table as not working, with what happened.

        The filename and the game are looked up here rather than taken from the
        caller: they are display context for a list the user reads months later,
        and the backend already holds both.
        """
        digest = self._sha(sha256)
        reason = _clamped_reason(reason)
        try:
            profile = None if app_id is None else self.profile_store.get(app_id)
        except (OSError, ValueError):
            profile = None
        # Resolved before the lock: reading the live process table and a PE
        # version header is observation, and holding every other mutation behind
        # it for the duration buys nothing.
        game_version = self._observed_game_version(profile)
        with self._mutation_lock:
            try:
                table = self.table_store.get_table(digest)
            except (OSError, ValueError):
                # A table can be blocked after its own file was already removed,
                # and the record is about the bytes, not about the copy on disk.
                table = None
            self._compatibility_pending.clear()
            self._startup_compatibility_pending.clear()
            # The cheat did not work, and that is true whether or not there is
            # room to write it down. The green this table was showing is about a
            # build it no longer works on, so it goes first: a full advisory list
            # is a reason the user has to be told about, never a reason to leave
            # a table the user has just watched fail reading as proven.
            try:
                self.table_compatibility.invalidate(digest)
            except (OSError, ValueError) as exc:
                log_activity(self.logger, "warning", "table_compatibility.invalidation_failed", table_sha=digest[:12], reason=str(exc)[:256])
            entry = self.table_blocklist.block(
                sha256=digest,
                reason=reason,
                filename=None if table is None else table.get("filename"),
                app_id=app_id,
                game_name=None if profile is None else profile.name,
                game_version=game_version,
                table_version=_advertised_release(table),
                origins=_provider_row_keys(table),
                cause=CAUSE_REFUSED,
            )

        log_activity(
            self.logger, "info", "table_blocklist.recorded",
            table_sha=digest[:12], cause="refused_by_cheat_engine",
        )
        return entry.as_dict()

    def _game_fingerprint(self, profile):
        path = self._observed_game_executable_path(profile)
        return {
            "pe_version": None if path is None else self._read_pe_version(path),
            "steam_build_id": None if profile is None or profile.is_shortcut else steam_build_id(self.paths.user_home, profile.app_id),
            # Kept with the evidence so the same question can be asked again
            # once the game has stopped. It is knowable only here, while the
            # game is up, and without it a non-Steam shortcut has nothing
            # comparable at all the moment it closes.
            "executable_path": path,
        }

    def _observed_game_executable_path(self, profile):
        """The absolute path of the running game's own target executable."""
        if profile is None or not profile.target_process:
            return None
        return observe_game_executable_path(profile.app_id, profile.target_process)

    @staticmethod
    def _read_pe_version(path):
        try:
            return read_pe_version(Path(path))
        except (OSError, ValueError):
            return None

    def _compatibility_fingerprints(self, profiles, current_app_id=None, recorded_paths=None):
        paths = observe_game_executable_paths({p.app_id: p.target_process for p in profiles.values() if p.target_process})
        try:
            _, libraries = discover_steam_library_roots(self.paths.user_home)
        except (OSError, ValueError, RuntimeError):
            libraries = []
        fingerprints = {}
        versions = {}
        recorded_paths = recorded_paths or {}
        relevant = set(paths)
        if current_app_id in profiles:
            relevant.add(current_app_id)
        # A game that is not running is still worth a fingerprint when its own
        # evidence says where to read one from.
        relevant.update(app_id for app_id, _ in recorded_paths if app_id in profiles)
        for app_id in relevant:
            profile = profiles[app_id]
            # The running game's own executable where it is running, and
            # otherwise the path its evidence was recorded from. The second is
            # what makes a closed game comparable at all: `pe_version` is read
            # off an executable, and a non-Steam shortcut has no build id, so
            # without it a shortcut's proven table read as "current build
            # unknown" from the moment the game stopped, which is the state
            # Manage is usually opened in. A path that has since been moved or
            # uninstalled simply reads as no version, exactly as before.
            #
            # Looked up by the program as well as the game, because one game can
            # have been proven on two of them - a launcher and the game it
            # starts - and the newest of those records is not necessarily about
            # the one in front of the reader. Reading a launcher's version and
            # comparing a table proven on the game against it is a retest nobody
            # owes, or, where a build stamps both files alike, a match nothing
            # checked.
            path = paths.get(app_id) or recorded_paths.get((app_id, (profile.target_process or "").casefold()))
            if path and path not in versions:
                try:
                    versions[path] = read_pe_version(Path(path))
                except (OSError, ValueError):
                    versions[path] = None
            fingerprints[app_id] = {
                "pe_version": versions.get(path),
                "steam_build_id": None if profile.is_shortcut else steam_build_id(self.paths.user_home, app_id, libraries=libraries),
            }
        return fingerprints

    def _compatibility_snapshot(self, current_app_id=None):
        snapshot = self.table_compatibility.snapshot()
        if not snapshot["entries"]:
            return snapshot
        wanted = {entry["app_id"] for entry in snapshot["entries"]}
        # Where each app's own evidence says its executable was, so a game that
        # is not running can still be compared. Newest per app and program,
        # because that is the record whose path was most recently true for the
        # thing being compared - a game proven on two programs holds a path for
        # each, and only one of them is about the target in the profile.
        recorded_paths = {}
        for entry in sorted(snapshot["entries"], key=lambda row: row["last_working_at"]):
            if entry.get("executable_path"):
                recorded_paths[(entry["app_id"], entry["target_process"].casefold())] = entry["executable_path"]
        try:
            profiles = {p.app_id: p for p in self.profile_store.list_profiles() if p.app_id in wanted}
            fingerprints = self._compatibility_fingerprints(profiles, current_app_id, recorded_paths)
        except (OSError, ValueError):
            profiles, fingerprints = {}, {}
        try:
            floor, epochs = self.table_blocklist.failure_epochs()
        except (OSError, ValueError):
            floor, epochs = None, {}
        for entry in snapshot["entries"]:
            if floor is None or entry["failure_epoch"] != epochs.get(entry["table_sha256"], floor):
                entry["invalidated"] = True
            profile = profiles.get(entry["app_id"])
            current = fingerprints.get(entry["app_id"], {})
            if profile is None or not profile.target_process or profile.target_process.casefold() != entry["target_process"].casefold():
                current = {}
            entry["state"] = compatibility_state(entry, current)
            # The path is what the comparison above was made from, and the
            # comparison is made here. Nothing on the panel reads it, so it is
            # dropped rather than published: it is an absolute path into the
            # user's game library, the panel's own record is collected into an
            # archive attached to public issues, and a field nothing reads is a
            # field that cannot justify being there. The state file keeps it,
            # which is where a support bundle can still show what was compared.
            entry.pop("executable_path", None)
        return snapshot

    def _observe_startup_compatibility(self, app_id, envelope):
        pending = self._startup_compatibility_pending.get(app_id)
        if pending is None:
            return
        prepared, status = envelope.get("prepared") or {}, envelope.get("status") or {}
        if prepared.get("session_id") != pending["session"]:
            self._startup_compatibility_pending.pop(app_id, None)
            return
        if not envelope.get("connected"):
            return
        startup_state = status.get("startup_state")
        if startup_state in ("failed", "failed_rolled_back", "failed_partial"):
            self._startup_compatibility_pending.pop(app_id, None)
            return
        if startup_state != "applied" or not status.get("attached") or status.get("table_load_state") != "loaded":
            return
        if status.get("startup_completed") != pending["total"] or status.get("startup_total") != pending["total"]:
            self._startup_compatibility_pending.pop(app_id, None)
            return
        proved = next((record_id for record_id in status.get("startup_active_ids", ()) if record_id in pending["leaves"]), None)
        digest = pending["digest"]
        floor, epochs = self.table_blocklist.failure_epochs()
        # A record about the bytes or about the source they came from is not an
        # answer to whether this table works, so it neither dates the evidence
        # nor stands between a successful startup and a fresh proof of it.
        if (proved is None or epochs.get(digest, floor) != pending["epoch"]
                or is_compatibility_failure(self.table_blocklist.find(digest))):
            self._startup_compatibility_pending.pop(app_id, None)
            return
        profile = self.profile_store.get(app_id)
        # Evidence is about a named process: without one there is nothing a
        # later status could compare this row against.
        if (profile is None or profile.table_sha256 != digest or not profile.target_process
                or profile.target_process.casefold() != (status.get("target_process") or "").casefold()):
            self._startup_compatibility_pending.pop(app_id, None)
            return
        self.table_compatibility.record(app_id, digest, profile.target_process, self._game_fingerprint(profile), failure_epoch=pending["epoch"])
        self._startup_compatibility_pending.pop(app_id, None)
        log_activity(self.logger, "info", "table_compatibility.startup_confirmed", app_id=app_id,
                     session=pending["session"][:12], table_sha=digest[:12], record_id=proved)

    def _observe_compatibility(self, app_id, envelope):
        # Only mutations accepted in this backend lifetime can earn a new mark.
        # A restart drops pending proof rather than reconstructing intent from
        # an active record alone. Nothing here authorizes table execution.
        with self._mutation_lock:
            self._observe_startup_compatibility(app_id, envelope)
            prepared = envelope.get("prepared") or {}
            session = prepared.get("session_id")
            pending = self._compatibility_pending.get(app_id)
            if pending is None:
                return
            if pending["session"] != session or not envelope.get("connected"):
                self._compatibility_pending.pop(app_id, None)
                return
            status = envelope.get("status") or {}
            if not status.get("attached") or status.get("table_load_state") != "loaded":
                return
            latest = {}
            for result in status.get("results", []):
                rid = result.get("record_id")
                if rid not in latest or result["generation"] > latest[rid]["generation"]:
                    latest[rid] = result
            for rid, proof in list(pending["records"].items()):
                observed = latest.get(rid)
                if not observed:
                    continue
                if observed["generation"] == proof["activation"]:
                    if observed.get("ok") and observed.get("active") is True:
                        proof["acknowledged"] = True
                    else:
                        pending["records"].pop(rid, None)
                elif proof["acknowledged"] and observed["generation"] == proof["query"]:
                    proof["verified"] = bool(observed.get("ok") and observed.get("active") is True)
                    if not proof["verified"]:
                        pending["records"].pop(rid, None)
            if not pending["records"]:
                self._compatibility_pending.pop(app_id, None)

    def confirm_table_working(self, app_id: int, digest: str, session_id: str, record_id: int) -> bool:
        """Finalize a successful UI operation using backend-held readback proof."""
        digest = self._sha(digest)
        if type(record_id) is not int or not 0 <= record_id <= 0x7FFFFFFF:
            raise ValueError("invalid compatibility record ID")
        with self._mutation_lock:
            runtime = self.get_runtime_status(app_id)
            prepared = runtime.get("prepared") or {}
            pending = self._compatibility_pending.get(app_id)
            if not runtime.get("connected") or prepared.get("session_id") != session_id or prepared.get("table_sha256") != digest:
                raise ValueError("compatibility proof session changed")
            proof = pending["records"].get(record_id) if pending else None
            if not proof or not proof.get("verified") or is_compatibility_failure(self.table_blocklist.find(digest)):
                return False
            status = runtime.get("status") or {}
            observed_process = status.get("target_process")
            if status.get("attached") is not True or not observed_process:
                return False
            profile = self.profile_store.get(app_id)
            observed_profile = None if profile is None else replace(profile, target_process=observed_process)
            floor, epochs = self.table_blocklist.failure_epochs()
            self.table_compatibility.record(app_id, digest, observed_process, self._game_fingerprint(observed_profile),
                                            failure_epoch=epochs.get(digest, floor))
            self._compatibility_pending.pop(app_id, None)
            log_activity(self.logger, "info", "table_compatibility.confirmed",
                         app_id=app_id, table_sha=digest[:12], session=session_id[:12], record_id=record_id,
                         generation=proof["query"])
            return True

    def _observed_game_version(self, profile) -> str | None:
        """The version the game's own executable declares, while it is running.

        A table stops working because the game was updated, so the build it was
        tried against is what dates the record - and it is only observable while
        that build is running, from the executable the game itself reports.
        Anything ambiguous or unreadable is left out rather than guessed: an
        absent version is honest, a wrong one is worse than none.

        Composed from the two halves rather than repeating them: finding the
        running executable and reading a version out of it are each wanted on
        their own now that the path is kept with the evidence.
        """
        path = self._observed_game_executable_path(profile)
        return None if path is None else self._read_pe_version(path)

    def unblock_table(self, sha256: str) -> bool:
        """Clear one record, by its digest or by the row a digest-less one holds.

        A record for a file the source no longer has never produced bytes, so it
        has no digest and is identified by its provider row. It is in the same
        list the user reads, so it is cleared by the same action.
        """
        key = sha256.strip() if isinstance(sha256, str) and sha256.startswith("row:") else self._sha(sha256)
        with self._mutation_lock:
            removed = self.table_blocklist.unblock(key)
        if removed:
            log_activity(self.logger, "info", "table_blocklist.cleared", table_sha=key[:20])
        return removed

    def clear_blocked_tables(self) -> int:
        with self._mutation_lock:
            removed = self.table_blocklist.clear()
        if removed:
            log_activity(self.logger, "info", "table_blocklist.cleared_all", count=removed)
        return removed

    def delete_table(self, sha256: str) -> dict[str, object]:
        """Remove one stored table on an explicit press, unless a game is on it.

        Deliberately narrow. The bytes are content-addressed and this is the
        only route that destroys one table on its own, the widest scope of
        `Prepare for removal` being the other way they can go, so it refuses the
        one case a user cannot undo by pressing something else: a table some
        game currently has selected. That game may be running it right now, its authorization is
        recorded against these exact bytes, and the panel would be left offering
        a table whose file is gone.

        Every other association is left to fall away on its own. A game's
        library entry for bytes that are no longer here lists as unavailable and
        is already filtered out of what the panel offers, so nothing is
        rewritten across profiles to make one deletion look tidy.
        """
        digest = self._sha(sha256)
        with self._mutation_lock:
            holders = [
                profile for profile in self.profile_store.list_profiles()
                if profile.table_sha256 == digest
            ]
            if holders:
                names = ", ".join(sorted({profile.name for profile in holders})[:3])
                raise ValueError(
                    f"this table is the selected table for {names}. "
                    "Stop using it there first, then delete it."
                )
            removed = self.table_store.delete_table(digest)
        log_activity(
            self.logger, "info", "table_store.deleted",
            table_sha=digest[:12], bytes=removed.get("size", 0),
        )
        return removed

    def inspect_table_source(self, selection: str) -> dict[str, object]:
        return self.table_store.inspect_source(selection, sevenzip=self._find_7zip())

    def import_table(
        self,
        selection: str,
        member_path: str | None = None,
        password: str | None = None,
        app_id: int | None = None,
    ) -> dict[str, object]:
        with self._mutation_lock:
            artifact = self.table_store.import_selection(
                selection,
                member_path=member_path,
                password=password,
                sevenzip=self._find_7zip(),
                # Only ever handed back to the not-working record, if this file
                # turns out not to be a table: the list is read months later and
                # a bare file name does not say what a table was for. It is
                # carried rather than resolved here, because the record is the
                # one place that needs a name and the ordinary import must not
                # pay a profile read for a write it will not make.
                app_id=app_id,
            )
            log_activity(
                self.logger, "info", "table_import.completed",
                table_sha=artifact.sha256[:12], filename=artifact.filename,
            )
            return artifact.as_dict()

    def _inspect_table_blob(self, blob: bytes, digest: str, app_id: int | None = None):
        """Read one exact table, and record it as not working if it cannot be.

        An inspection refusal is a property of these exact bytes and of nothing
        else: the file is on disk, it is the file it says it is, and no state of
        this device changes what the parser makes of it. That is the same thing
        the durable record already says about a download that turns out not to
        be a table, and it was the one way a table could be unusable without
        being written down anywhere.

        Observed on this device with a 1.4 MB table of 323 entries whose item
        list one record exceeded a parser limit with: the table imported, joined
        the game's library and was offered as `Local` in every later search,
        and every press ended in the same refusal with nothing to clear and no
        way back. It is recorded now, so search greys the row with the reason,
        re-import is refused with it, and **Tables that did not work** offers
        the press that undoes all of that.
        """
        try:
            return inspect_table(blob, digest)
        except TableBlobUnavailable:
            # Nothing about a table: the stored file is gone, unreadable or not
            # the bytes this digest names, which is a state of this device. A
            # table nobody has a copy of any more must not be written down as
            # one that does not work.
            raise
        except ValueError as exc:
            try:
                stored = self.table_store.get_table(digest)
            except (OSError, ValueError):
                stored = None
            filename = stored.get("filename") if isinstance(stored, dict) else None
            self._record_unusable_table(
                digest,
                f"Cheat Engine's table could not be read: {exc}",
                filename if isinstance(filename, str) and filename else f"{digest[:12]}.CT",
                _provider_row_keys(stored),
                app_id,
                table_version=_advertised_release(stored),
            )
            raise

    def inspect_table_sha(self, digest: str, app_id: int | None = None) -> dict[str, object]:
        digest = self._sha(digest)
        blob = self.table_store.verified_blob(digest)
        inspection = self._inspect_table_blob(blob, digest, app_id)
        # What the parser made of this exact file: how many entries it holds,
        # how many of them CE Decky can offer, and what it could not read. The
        # panel re-inspects on every context refresh, so this is recorded once
        # per table and again only if the same digest ever parses differently.
        actionable = sum(1 for control in inspection.controls if control.id is not None)
        self._table_journal.observe(
            digest,
            (
                inspection.total_entries, len(inspection.controls), actionable,
                inspection.unsupported_record_id_count, len(inspection.ambiguous_record_ids),
                inspection.table_version, inspection.has_lua, inspection.has_auto_assembler,
                inspection.embedded_files, inspection.has_forms,
                inspection.sanitized_labels, inspection.dropped_values,
                inspection.dropped_value_lists,
            ),
            table_sha=digest[:12],
            entries=inspection.total_entries,
            controls=len(inspection.controls),
            actionable=actionable,
            unsupported_ids=inspection.unsupported_record_id_count,
            ambiguous_ids=len(inspection.ambiguous_record_ids),
            table_format=inspection.table_version,
            lua=inspection.has_lua,
            auto_assembler=inspection.has_auto_assembler,
            embedded_files=inspection.embedded_files,
            # A window the table brought with it, and what this could not take
            # exactly as written: a label with a spoofing character removed, and
            # a value the runtime cannot carry. Both used to refuse the table,
            # so both were previously visible only as a failed import.
            forms=inspection.has_forms,
            sanitized_labels=inspection.sanitized_labels,
            dropped_values=inspection.dropped_values,
            dropped_value_lists=inspection.dropped_value_lists,
            process_hints=",".join(inspection.process_candidates[:6]) or None,
        )
        return inspection.as_dict()

    def list_table_code(self, digest: str) -> dict[str, object]:
        """What one exact table carries that Cheat Engine can execute.

        Read-only, and it runs nothing. It exists because the decision this
        product asks a user to make is whether these exact bytes may execute,
        and the only answer they had to make it on was a count of the markers.
        Reading the scripts themselves meant Desktop Mode, which a user in Game
        Mode does not have.
        """
        digest = self._sha(digest)
        return read_code_index(self.table_store.verified_blob(digest), digest)

    def read_table_code(self, digest: str, section_id: str) -> dict[str, object]:
        """One section of that table, bounded and normalized for the panel."""
        digest = self._sha(digest)
        return read_code_section(self.table_store.verified_blob(digest), digest, section_id)

    def list_profiles(self) -> list[dict[str, object]]:
        return [profile.as_dict() for profile in self.profile_store.list_profiles()]

    def get_provider_capabilities(self) -> list[dict[str, object]]:
        return [definition.as_dict() for definition in DEFAULT_PROVIDER_DEFINITIONS]

    def begin_close(self) -> None:
        """What unload must do before it is allowed to suspend.

        `docs/FIELD_NOTES.md` records what Decky's stop does to this process:
        it signals it and closes its socket, both ends of that socket then read
        at EOF in a loop that never suspends, the event loop is starved, and
        SIGKILL arrives five seconds later. Everything after the first `await`
        of an unload is therefore work that may simply never happen.

        So the owners that have something which must happen do it here, in this
        thread, and `close()` below stays what it was: the orderly version, for
        the callers that still have a loop. Neither one is allowed to fail the
        unload, and running both is deliberate, because each of these is
        idempotent by construction.
        """
        # The launch supervisor first, and the order is the point. Its prologue
        # is bounded by construction and what it does is irreversible if it does
        # not happen: an owned Cheat Engine left running outlives this process
        # with nothing naming it. The catalog's is one small atomic write, which
        # is cheap and is still second: durability of a marker never goes ahead
        # of a process that must be stopped. That write is small deliberately.
        # It used to be the whole index cache, a third of a megabyte behind a
        # lock a native writer holds across its own `fsync`, so it had to be
        # bounded, and a bounded write is one that can decline to happen.
        for owner, begin in (
            ("ce_launch", self.ce_launch.begin_close),
            ("provider_catalog", self.provider_catalog.begin_close),
        ):
            try:
                begin()
            except Exception as exc:  # noqa: BLE001 - a prologue never fails an unload
                log_failure(
                    self.logger, "backend.owner_begin_close_failed", exc,
                    expected=isinstance(exc, (OSError, RuntimeError, ValueError)), owner=owner,
                )
        # One line, whether or not anything was owed. The owners above are quiet
        # when they have nothing to do, and a prologue that is silent when it
        # succeeds is indistinguishable on the device from one that was never
        # reached, which is the exact confusion this whole surface exists to end.
        log_activity(self.logger, "info", "backend.begin_close_completed")

    async def close(self) -> None:
        # Owned Cheat Engine processes must be retired before the plugin can
        # claim it released everything it started. A failure in one owner must
        # not skip draining the remaining owners: asyncio.to_thread workers keep
        # mutating after their awaiter is abandoned. Preserve the first failure
        # after every bounded close has had a chance to finish.
        #
        # Each owner is recorded as it is entered and as it is left, because the
        # one outcome this loop cannot report for itself is an owner that never
        # returns. Decky gives a plugin five seconds to stop and then sends
        # SIGKILL, so a close that blocks leaves no record of its own at all:
        # the journal ends on whatever was written before it, and which owner
        # was holding the unload has to be guessed from that gap. It is two
        # records per owner on a path taken once per plugin load, and it is what
        # makes the difference between a readable hang and an invisible one.
        failure: Exception | None = None
        cancelled = False
        for owner, close in (
            ("ce_launch", self.ce_launch.close),
            ("managed_ce", self.managed_ce.close),
            # The provider catalog owns the resumable FearLess background index;
            # stop and drain its bounded cache writes before shared HTTP closes.
            ("provider_catalog", self.provider_catalog.close),
            ("acquisitions", self.acquisitions.close),
        ):
            started = time.monotonic()
            log_activity(self.logger, "info", "backend.owner_close_started", owner=owner)
            outcome = "completed"
            try:
                await close()
            except asyncio.CancelledError:
                # An owner that drained through cancellation re-raises it. That
                # is not permission to skip the owners after it: cancellation
                # is preserved once every bounded close has run.
                cancelled = True
                outcome = "cancelled"
            except Exception as exc:
                outcome = "failed"
                log_failure(
                    self.logger, "backend.owner_close_failed", exc,
                    expected=isinstance(exc, (OSError, RuntimeError, ValueError)),
                    owner=owner,
                )
                if failure is None:
                    failure = exc
            log_activity(
                self.logger, "info", "backend.owner_close_ended",
                owner=owner, outcome=outcome, duration_ms=int((time.monotonic() - started) * 1000),
            )
        if cancelled:
            raise asyncio.CancelledError
        if failure is not None:
            raise failure

    async def search_tables(self, game_identity: dict[str, object], progress_token: str | None = None) -> dict[str, object]:
        if not isinstance(game_identity, dict) or set(game_identity) - {"display_name", "shortcut_executable"}:
            raise ValueError("game identity is malformed")
        display_name = game_identity.get("display_name")
        executable = game_identity.get("shortcut_executable")
        if not isinstance(display_name, str):
            raise ValueError("game identity display_name must be a string")
        if executable is not None and not isinstance(executable, str):
            raise ValueError("game identity shortcut_executable must be a string or null")
        return await self.provider_catalog.search(display_name, executable, _search_token(progress_token))

    def poll_table_search(self, progress_token: str) -> dict[str, object] | None:
        """What this caller's own search is doing, for the screen waiting on it."""
        token = _search_token(progress_token)
        if token is None:
            raise ValueError("search progress token is required")
        return self.provider_catalog.search_progress(token)

    async def start_table_acquisition(self, provider: str, artifact_id: str, search_id: str | None = None, app_id: int | None = None) -> dict[str, object]:
        return await self.acquisitions.start(provider, artifact_id, search_id, app_id)

    def poll_table_acquisition(self, acquisition_id: str) -> dict[str, object]:
        return self.acquisitions.poll(acquisition_id)

    async def wait_table_acquisition(self, acquisition_id: str) -> None:
        await self.acquisitions.wait_ready(acquisition_id)

    def complete_table_acquisition(self, acquisition_id: str, picked_path: str | None = None, member_path: str | None = None, password: str | None = None) -> dict[str, object]:
        with self._mutation_lock:
            return self.acquisitions.complete_blocking(acquisition_id, picked_path, member_path, password)

    async def cancel_table_acquisition(self, acquisition_id: str) -> dict[str, object]:
        return await self.acquisitions.cancel(acquisition_id)

    def get_managed_ce_capability(self) -> dict[str, object]:
        with self._mutation_lock:
            capability = self.managed_ce.capability()
            operation = capability.get("operation")
            if isinstance(operation, dict):
                self._reconcile_managed_ce_reservation(operation)
            elif self._managed_ce_reservation not in {None, "starting"}:
                # No manager operation remains, so a reservation cannot still
                # authorize a mutation boundary.  Preserve the short-lived
                # pre-operation sentinel while async start is creating its
                # native extraction transaction.
                self._managed_ce_reservation = None
            return capability

    async def start_managed_ce_install(self, force: bool = False) -> dict[str, object]:
        if not isinstance(force, bool):
            raise ValueError("managed CE reinstall flag must be a boolean")
        with self._mutation_lock:
            self._assert_no_managed_ce_transition()
            for profile in self.profile_store.list_profiles():
                self._assert_no_live_owned_launch(profile.app_id)
            # Reserve before releasing the shared mutation boundary.  The
            # manager's async start creates its transaction before it can publish
            # a real operation id; without this sentinel a concurrent blocking
            # RPC can enter that gap and race extraction promotion.
            self._managed_ce_completion = None
            self._managed_ce_reservation = "starting"
        try:
            operation = await self.managed_ce.start(force=force)
        except BaseException:
            with self._mutation_lock:
                if self._managed_ce_reservation == "starting":
                    self._managed_ce_reservation = None
            raise
        with self._mutation_lock:
            operation_id = self._operation_id(str(operation.get("operation_id", "")))
            if self._managed_ce_reservation != "starting":
                raise RuntimeError("managed CE setup reservation was lost")
            self._managed_ce_reservation = operation_id
            self._reconcile_managed_ce_reservation(operation)
        return operation

    def poll_managed_ce_install(self, operation_id: str) -> dict[str, object]:
        operation_id = self._operation_id(operation_id)
        with self._mutation_lock:
            # A consumed operation is reported as unknown on purpose.  Serving a
            # sticky `completed` snapshot here lets a superseded frontend monitor
            # repaint terminal setup progress after the winning monitor already
            # cleared it, which blocks the whole home workflow.  The completion
            # receipt below keeps the losing observer's *completion* read-only;
            # its poll is reconciled against the capability instead.
            operation = self.managed_ce.status(operation_id)
            self._reconcile_managed_ce_reservation(operation)
            return operation

    async def wait_managed_ce_install(self, operation_id: str) -> None:
        await self.managed_ce.wait(self._operation_id(operation_id))

    async def cancel_managed_ce_install(self, operation_id: str) -> dict[str, object]:
        operation_id = self._operation_id(operation_id)
        operation = await self.managed_ce.cancel(operation_id)
        with self._mutation_lock:
            self._reconcile_managed_ce_reservation(operation)
        return operation

    def complete_managed_ce_install(self, operation_id: str) -> dict[str, object]:
        operation_id = self._operation_id(operation_id)
        with self._mutation_lock:
            if self._managed_ce_reservation != operation_id:
                if self._managed_ce_completion is not None and self._managed_ce_completion[0] == operation_id:
                    return {**self._managed_ce_completion[1], "completed_now": False}
                raise ValueError("managed CE setup changed; refresh before completing it")
            for profile in self.profile_store.list_profiles():
                self._assert_no_live_owned_launch(profile.app_id)
            installed = self.managed_ce.completed_install(operation_id)
            config = self._config_for_ce_identity_write()
            config.imported_ce_executable = installed.executable
            config.imported_ce_root = installed.root
            config.imported_ce_sha256 = installed.sha256
            config.imported_ce_version = read_pe_version(Path(installed.executable))
            self.config_store.save(config)
            self.managed_ce.consume_completed(operation_id)
            self._managed_ce_reservation = None
            completion = installed.as_dict()
            self._managed_ce_completion = (operation_id, completion)
            log_activity(
                self.logger, "info", "managed_ce.registered",
                executable_sha=installed.sha256[:12], path=installed.executable,
            )
            return {**completion, "completed_now": True}

    def plan_provider_search(self, display_name: str, shortcut_executable: str | None = None) -> dict[str, object]:
        """Build the bounded local provider query plan without performing network I/O."""
        aliases = game_aliases(display_name, shortcut_executable)
        queries = query_plan(display_name, shortcut_executable)
        disabled = self._disabled_provider_ids()
        return {
            "aliases": aliases,
            "queries": queries,
            # The providers this search will actually run, which is the same
            # answer the search itself is built from. A provider that is enabled
            # but reached only as another provider's linked source is a
            # capability, not a plan step, and listing it here described a
            # search that was never going to happen. A source the user switched
            # off is left out for the same reason.
            "providers": [
                definition.as_dict() for definition in searchable_providers()
                if definition.provider not in disabled
            ],
            "switched_off": sorted(disabled),
            "network_enabled": True,
            "requires_target_validation": True,
        }

    def evaluate_provider_candidates(
        self,
        display_name: str,
        shortcut_executable: str | None,
        candidates: list[dict[str, object]],
        desired_platform: str | None = None,
    ) -> dict[str, object]:
        """Apply local identity ranking to structured provider candidates only."""
        if not isinstance(candidates, list):
            raise ValueError("provider candidates must be a list")
        parsed: list[Candidate] = []
        allowed = {"title", "provider_trust", "platform_tags", "has_artifact", "provider_id", "release_year"}
        for raw in candidates:
            if not isinstance(raw, dict) or set(raw) - allowed or "title" not in raw:
                raise ValueError("provider candidate is malformed")
            tags = raw.get("platform_tags", ())
            if isinstance(tags, list):
                tags = tuple(tags)
            parsed.append(Candidate(
                title=raw["title"],  # type: ignore[arg-type]
                provider_trust=raw.get("provider_trust", 0.5),  # type: ignore[arg-type]
                platform_tags=tags,  # type: ignore[arg-type]
                has_artifact=raw.get("has_artifact", True),  # type: ignore[arg-type]
                provider_id=raw.get("provider_id"),  # type: ignore[arg-type]
                release_year=raw.get("release_year"),  # type: ignore[arg-type]
            ))
        decision = decide_match(game_aliases(display_name, shortcut_executable), parsed, desired_platform)
        return {
            "action": decision.action.value,
            "reason": decision.reason,
            "ranked": [
                {"candidate": asdict(item.candidate), "match": asdict(item.match)}
                for item in decision.ranked
            ],
        }

    def get_provider_diagnostics(self) -> dict[str, object]:
        return self.provider_diagnostics.snapshot()

    def clear_provider_diagnostics(self, provider_id: str) -> dict[str, object]:
        with self._mutation_lock:
            return {"cleared": self.provider_diagnostics.clear(provider_id)}

    async def reset_provider_diagnostics(self) -> dict[str, object]:
        """Replace an unusable provider-counter record and report the new state.

        Using the plugin does not repair this one. Every recording path loads
        the whole file first, a corrupt load raises, and every caller swallows
        that on purpose so a broken counter can never fail a search - so the
        only account of what each source is doing would stay broken for good
        while the screen showing it said searches would rebuild it.

        Searches are drained first, for the reason the cache deletion drains
        them: a search that started before this is a writer of the very record
        being replaced, and it would put its own counters back into the new one.
        The quiesce is inside the same try as the reset, so cancelling this
        cannot leave searches refused for a repair that never ran.
        """
        try:
            await self.provider_catalog.suspend_searches()
            with self._mutation_lock:
                replaced = self.provider_diagnostics.reset()
                snapshot = self.get_provider_sources()
        finally:
            self.provider_catalog.resume_searches()
        log_activity(
            self.logger, "info", "provider_diagnostics.reset",
            had_entries=replaced, unreadable=snapshot["diagnostics_reason"] is not None,
        )
        return snapshot

    def _disabled_provider_ids(self) -> frozenset[str]:
        """The switched-off sources, and never an exception.

        The catalog calls this on the path of every search, so a record that
        cannot be read must not be able to fail one. It reads as no refusals,
        exactly as an unreadable blocklist refuses nothing, and the reason is
        carried instead to Advanced, the diagnostics snapshot and the support
        bundle, where it can be seen and repaired.
        """
        try:
            disabled = self.provider_sources.disabled()
        except (OSError, ValueError) as exc:
            reason = str(exc)[:448]
            if reason != self._provider_sources_reason:
                self._provider_sources_reason = reason
                log_activity(
                    self.logger, "warning", "provider_sources.unreadable", reason=reason,
                )
            return frozenset()
        if self._provider_sources_reason is not None:
            self._provider_sources_reason = None
            log_activity(self.logger, "info", "provider_sources.readable_again")
        return disabled

    def get_provider_sources(self) -> dict[str, object]:
        """Every source the user can switch, what it is doing, and what it did.

        One answer rather than two, because the screen that offers the switch is
        the screen that has to justify it: a source is switched off after it has
        been seen failing, timing out or returning nothing, and that evidence
        already exists in the provider diagnostics. Splitting them across two
        calls left the panel joining a registry to a diagnostics file by hand.
        """
        selection = self.provider_sources.snapshot()
        disabled = {item for item in selection["disabled"] if isinstance(item, str)}
        try:
            diagnostics = self.provider_diagnostics.snapshot()["providers"]
            diagnostics_reason = None
        except (OSError, ValueError) as exc:
            diagnostics, diagnostics_reason = {}, str(exc)[:448]
        now = time.time()
        sources: list[dict[str, object]] = []
        for definition in selectable_providers():
            state = diagnostics.get(definition.provider)
            state = state if isinstance(state, dict) else {}
            counters = state.get("counters")
            cooldown_until = state.get("cooldown_until_epoch_s")
            cooldown = 0
            if isinstance(cooldown_until, (int, float)) and not isinstance(cooldown_until, bool):
                cooldown = max(0, int(cooldown_until - now))
            sources.append({
                "provider": definition.provider,
                "provider_display_name": definition.provider_display_name,
                "priority": definition.priority,
                "discovery": definition.discovery,
                "linked_target": definition.linked_target,
                "enabled": definition.provider not in disabled,
                # Absent rather than zeroed when this source has never been
                # asked for anything: nothing recorded and everything recorded
                # as zero are different facts, and a screen that shows them
                # alike cannot say which sources have actually been tried.
                "state": state.get("state") if state else None,
                "counters": counters if isinstance(counters, dict) else None,
                "last_error": state.get("last_error"),
                "last_http_status": state.get("last_http_status"),
                "last_latency_ms": state.get("last_latency_ms"),
                "last_throttle_wait_s": state.get("last_throttle_wait_s"),
                "cooldown_seconds": cooldown,
            })
        return {
            "schema": 1,
            "sources": sources,
            "enabled_count": sum(1 for item in sources if item["enabled"]),
            "total": len(sources),
            "updated_at": selection["updated_at"],
            "selection_reason": selection["reason"],
            "diagnostics_reason": diagnostics_reason,
        }

    def set_provider_enabled(self, provider_id: str, enabled: object) -> dict[str, object]:
        """Switch one source on or off, and say what the whole set now is.

        Only a provider this build can actually reach is switchable. Accepting
        any well-formed ID would let the record accumulate refusals of sources
        that do not exist, which are then indistinguishable from the refusal of
        a source a later version removed and restored.
        """
        provider = normalized_provider_id(provider_id)
        if not isinstance(enabled, bool):
            raise ValueError("provider enablement must be boolean")
        if provider not in {definition.provider for definition in selectable_providers()}:
            raise ValueError("provider is not a switchable table source")
        with self._mutation_lock:
            changed = self.provider_sources.set_enabled(provider, enabled)
            if not enabled:
                # Switching a source off stops this device talking to it, which
                # includes the request a background index refresh already has
                # out. Checking the switch before starting one, and again before
                # writing anything down, does not cover the one in flight.
                self.provider_catalog.stop_provider_background_work(provider)
            snapshot = self.get_provider_sources()
        log_activity(
            self.logger, "info", "provider_sources.set",
            provider=provider, enabled=enabled, changed=changed,
            enabled_count=snapshot["enabled_count"], total=snapshot["total"],
        )
        return snapshot

    def reset_provider_sources(self) -> dict[str, object]:
        """Switch every source back on, readable record or not."""
        with self._mutation_lock:
            removed = self.provider_sources.clear()
            self._provider_sources_reason = None
            snapshot = self.get_provider_sources()
        log_activity(self.logger, "info", "provider_sources.reset", restored=removed)
        return snapshot

    def record_panel_log(
        self,
        entries: object = None,
        dropped: object = 0,
        session: object = "",
    ) -> dict[str, object]:
        """Take what the panel has recorded since its last flush, and keep it.

        The panel's own record lives in the renderer, and recovering a wedged
        Quick Access panel restarts Steam's webhelper, which destroys it. A
        bundle collected after that recovery has no frontend evidence at all,
        which is exactly the case the evidence is wanted for. So the panel hands
        its entries over as it goes and this writes them down.

        This is diagnostics and never a press the user is waiting on: every
        failure is absorbed and reported in the answer, so a full or read-only
        filesystem cannot turn a flush into a panel that stops working. The
        panel does not retry a rejected flush either; the entries stay in its
        ring and the next flush carries them.
        """
        normalized = normalize_frontend_log(entries)
        if not normalized:
            return {"ok": True, "accepted": 0}
        path = frontend_journal.journal_path(self.paths.state_root)
        try:
            written = frontend_journal.append_entries(
                path,
                normalized,
                dropped=int(dropped) if isinstance(dropped, (int, float)) else 0,
                session=session if isinstance(session, str) else "",
            )
        except Exception as exc:  # noqa: BLE001 - diagnostics must never fail a panel
            log_activity(
                self.logger, "warning", "panel_log.not_recorded",
                error_type=type(exc).__name__, reason=str(exc)[:256], path=str(path),
            )
            return {"ok": False, "accepted": 0, "reason": type(exc).__name__}
        return {"ok": True, "accepted": written}

    def create_support_bundle(self, frontend_log: object = None, frontend_dropped: object = 0) -> dict[str, object]:
        """Write one archive that answers a bug report, and say where it is.

        Every snapshot is read here rather than inside the bundle module, so a
        probe that raises becomes a note in the manifest instead of the reason a
        user has nothing to attach: an unreadable profile store is very often the
        defect being reported, and it must not also be what prevents reporting
        it. The frontend's own bounded log arrives as an argument, because the
        panel is the only place that knows what was on screen.
        """
        def _try(produce, default=None):
            try:
                return produce()
            except Exception as exc:  # noqa: BLE001 - a failed probe is evidence
                log_activity(
                    self.logger, "warning", "support_bundle.probe_failed",
                    probe=getattr(produce, "__name__", "probe"),
                    error_type=type(exc).__name__, reason=str(exc)[:256],
                )
                return default

        status = _try(self.get_status)
        # The journal is where this plugin's backend records actually survive an
        # install, so it is read for every bundle. It runs a subprocess, so it is
        # read here with the other probes, where a failure becomes a manifest note.
        journal = _try(journal_records.collect) if journal_records.available() else None
        diagnostics = _try(self.diagnostics_snapshot)
        self_test = _try(self.self_test)
        inventory = _try(self.get_session_inventory)
        providers = _try(self.get_provider_diagnostics)
        # The sources the user actually left on, beside what each one did. A
        # report that a game finds no tables is unanswerable without it: an
        # empty answer from a source that was never asked and an empty answer
        # from a source that was asked and found nothing look identical in every
        # other file in this archive.
        provider_sources = _try(self.get_provider_sources)
        blocked = _try(self.list_blocked_tables)

        # Runtime state and launch capability are per game, and the launcher
        # scope answer is the one that reports Cheat Engine ownership at all.
        app_ids: list[int] = []
        if isinstance(status, dict) and isinstance(status.get("profiles"), list):
            for profile in status["profiles"]:
                if isinstance(profile, dict) and isinstance(profile.get("app_id"), int):
                    app_ids.append(profile["app_id"])
        runtime = [
            {"app_id": app_id, "envelope": _try(lambda app_id=app_id: self.get_runtime_status(app_id))}
            for app_id in app_ids[:24]
        ]
        launch = [{"app_id": None, "capability": _try(self.get_ce_launch_capability)}]
        launch += [
            {"app_id": app_id, "capability": _try(lambda app_id=app_id: self.get_ce_launch_capability(app_id))}
            for app_id in app_ids[:8]
        ]

        entries = normalize_frontend_log(frontend_log)
        dropped = frontend_dropped if isinstance(frontend_dropped, int) and not isinstance(frontend_dropped, bool) else 0
        result = write_support_bundle(
            self.paths,
            status=status,
            diagnostics=diagnostics,
            self_test=self_test,
            session_inventory=inventory,
            runtime=runtime,
            launch_capabilities=launch,
            provider_diagnostics=providers,
            provider_sources=provider_sources,
            blocked_tables=blocked,
            frontend_log=entries,
            frontend_dropped=max(0, dropped),
            version=__version__,
            journal=journal,
        )
        log_activity(
            self.logger, "info", "support_bundle.written",
            path=result.get("path"), size=result.get("size_bytes"),
            members=result.get("member_count"), notes=len(result.get("notes") or ()),
            frontend_entries=len(entries),
        )
        return result

    def get_session_inventory(self) -> dict[str, object]:
        return self.session_store.inventory()

    def get_removal_readiness(self) -> dict[str, object]:
        blockers: list[str] = []
        try:
            profiles = self.profile_store.list_profiles()
            profile_state_error: str | None = None
        except ValueError as exc:
            profiles = []
            profile_state_error = str(exc)[:512]
            blockers.append("profile state is corrupt or unreadable")
        try:
            inventory = self.session_store.inventory()
            session_inventory_error: str | None = None
        except ValueError as exc:
            inventory = {"apps": [], "total_sessions": 0, "errors": []}
            session_inventory_error = str(exc)[:512]
            blockers.append("session inventory is corrupt or unreadable")

        current_sessions: list[int] = []
        session_errors: list[dict[str, object]] = []
        corrupt_entries = 0
        for app in inventory.get("apps", []):
            if not isinstance(app, dict):
                continue
            app_id = app.get("app_id")
            if isinstance(app_id, int) and not isinstance(app_id, bool):
                if app.get("current_session_id") is not None:
                    current_sessions.append(app_id)
                if app.get("current_error"):
                    session_errors.append({"app_id": app_id, "error": str(app.get("current_error"))[:512]})
            value = app.get("corrupt_entries", 0)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                corrupt_entries += value
        root_errors = inventory.get("errors", [])
        if isinstance(root_errors, list):
            for item in root_errors:
                if isinstance(item, dict):
                    session_errors.append({"app_id": 0, "error": f"{item.get('path', '?')}: {item.get('error', 'invalid session entry')}"[:512]})
        # A prepared session is plugin-owned state under the managed root, which
        # removal deletes along with everything else; having one is not a reason
        # to refuse. What actually makes deletion unsafe is a Cheat Engine
        # process CE Decky owns still executing out of that tree.
        try:
            live_owned = self.ce_launch.has_live_owned_launch()
        except Exception as exc:  # noqa: BLE001 - a failed probe must fail closed
            live_owned = True
            session_errors.append({"app_id": 0, "error": f"owned-process probe failed: {str(exc)[:256]}"})
        if live_owned:
            blockers.append("a Cheat Engine process CE Decky owns is still running; stop it first")
        if session_errors or corrupt_entries:
            blockers.append("session state contains corrupt or ambiguous entries")

        return {
            "directories": self._managed_directory_inventory(),
            "profiles_total": len(profiles),
            "current_session_app_ids": sorted(set(current_sessions)),
            "live_owned_launch": live_owned,
            "session_errors": session_errors,
            "session_corrupt_entries": corrupt_entries,
            "profile_state_error": profile_state_error,
            "session_inventory_error": session_inventory_error,
            "can_delete_managed_data": not blockers,
            "blockers": blockers,
            "managed_root": str(self.paths.managed_root),
            "requires_target_validation": True,
        }

    async def delete_managed_data(self, scope: str) -> dict[str, object]:
        """Delete what CE Decky put on disk, at one of three named scopes.

        This is the only destructive action the plugin offers, so nothing about
        it is taken from the caller except which of the three scopes to use. The
        paths come from `_managed_directories()` - the same list the panel showed
        before the user confirmed.

        Only work that is executing out of the tree refuses it: a Cheat Engine
        process CE Decky owns, a managed installation still writing into
        `ce_root`, or a download that still owns a staged file. The other things
        removal readiness reports - corrupt profile state, ambiguous session
        entries - are reasons to delete rather than reasons to refuse, and
        gating on them would trap the user in the state this escapes.

        The contents of each directory go, not the directory itself: Decky owns
        the settings and log directories, and the managed roots are re-created
        immediately so the next operation does not race a missing tree.

        A directory that could not be cleared is reported in the result, not
        raised: the rest of a confirmed deletion really happened, and saying
        nothing did would be the same lie the durable-write reconciliation was
        built to stop telling.
        """
        keys = MANAGED_DATA_SCOPES.get(scope) if isinstance(scope, str) else None
        if keys is None:
            raise ValueError("unknown managed-data deletion scope")
        # This owns its own worker boundary rather than going through
        # `run_blocking`, so it does not inherit that boundary's started and
        # failed records either. The only destructive action the plugin offers
        # is not the one to be missing from a support bundle, so it keeps its
        # own. A refusal is a completed decision and is recorded as one; an
        # exception is what nothing downstream can reconstruct.
        log_activity(self.logger, "info", "managed_data.delete_started", scope=scope)
        try:
            return await self._delete_managed_data(scope, keys)
        except ValueError as exc:
            log_activity(
                self.logger, "info", "managed_data.delete_refused", scope=scope, reason=str(exc)[:256],
            )
            raise
        except Exception as exc:  # noqa: BLE001 - recorded, then raised unchanged
            log_failure(self.logger, "managed_data.delete_failed", exc, expected=False, scope=scope)
            raise

    async def _delete_managed_data(self, scope: str, keys: tuple[str, ...]) -> dict[str, object]:
        # The provider index lives in memory as well as in the cache directory
        # and a background refresh writes it back, so it is held down for the
        # whole operation rather than merely stopped before it.
        # Refusals are re-checked authoritatively under the lock below, but a
        # deletion that is going to be refused must not disturb anything on the
        # way to being refused: suspending first cancelled a background index
        # pass for an operation that then did nothing at all.
        for refusal in await drained_to_thread(self._deletion_refusals):
            raise ValueError(refusal)
        # Starting a download does not pass through the mutation boundary, so a
        # single check cannot hold: one begun immediately after it would create
        # staging under the deletion. New ones are refused for the whole
        # transaction, and the authoritative re-check under the lock catches any
        # that were already in flight when this began.
        self.acquisitions.suspend()
        suspended_index = "cache" in keys
        # Entered before the first thing that can be awaited, and covering every
        # subsystem this may have suspended. Quiescing is two awaits of its own,
        # and cancellation at either of them used to leave downloads, searches
        # or the background index refused until the plugin was reloaded, for a
        # deletion that never ran. The resume calls are flag clears and are
        # safe for a subsystem this never reached.
        try:
            if suspended_index:
                # An interactive search writes the same files the background
                # crawl does - the results cache, the provider diagnostics, its
                # own snapshot authority - and closing the Search screen does
                # not stop it. Without this a search the user had already closed
                # could finish around the deletion and rebuild exactly what was
                # just removed, so "it is rebuilt on the next search" became "it
                # is rebuilt from the previous one".
                await self.provider_catalog.suspend_searches()
                await self.provider_catalog.suspend_index()
            deleted = await drained_to_thread(self._delete_managed_data_locked, keys)
        finally:
            self.acquisitions.resume()
            if suspended_index:
                self.provider_catalog.resume_index()
                self.provider_catalog.resume_searches()
        # Best effort, and deliberately after the point of no return. The files
        # are already gone; a readiness read that fails afterwards is a report
        # this could not produce, not a deletion that did not happen, and
        # raising it here rejected the whole call and told the panel nothing had
        # been removed - so the panel skipped the reconciliation it owes for
        # state that really is gone.
        readiness = None
        readiness_error = None
        try:
            readiness = await drained_to_thread(self.get_removal_readiness)
        except Exception as exc:  # noqa: BLE001 - a failed report is not a failed deletion
            readiness_error = str(exc)[:448]
            log_failure(
                self.logger, "managed_data.readiness_unavailable", exc, expected=True,
                scope=scope,
            )
        return {
            "scope": scope,
            "deleted": deleted,
            "failed": [item for item in deleted if item["error"] is not None],
            "readiness": readiness,
            "readiness_error": readiness_error,
        }

    def _delete_managed_data_locked(self, keys: tuple[str, ...]) -> list[dict[str, object]]:
        with self._mutation_lock:
            for refusal in self._deletion_refusals():
                raise ValueError(refusal)
            selected = [entry for entry in self._managed_directories() if entry[0] in keys]
            results = [_clear_managed_directory(key, label, path) for key, label, path, _ in selected]
            # Re-establish the tree before anything else runs, and forget what
            # the deleted files were backing, whether or not every directory
            # cleared. A failure to re-establish is reported against the
            # deletion that already happened rather than replacing it.
            try:
                self.paths.ensure()
            except (OSError, RuntimeError) as exc:
                results.append({
                    "key": "paths", "label": "Plugin directories",
                    "removed_files": 0, "removed_bytes": 0,
                    "error": f"could not be re-established: {str(exc)[:400]}",
                })
            self._reset_after_deletion(keys)
            return results

    def _deletion_refusals(self) -> list[str]:
        """Work that is executing out of the tree deletion would remove."""
        refusals: list[str] = []
        if self._live_owned_launch_for_deletion():
            refusals.append("a Cheat Engine process CE Decky owns is still running; stop it first")
        if self.managed_ce.has_active_operation() or self._managed_ce_reservation is not None:
            # The reservation exists exactly for the gap before the manager can
            # publish an operation. Asking only the manager let deletion through
            # that gap, which is the one moment the reservation was invented for.
            refusals.append("a Cheat Engine installation is still in progress; cancel it first")
        if self.acquisitions.has_active():
            refusals.append("a table download is still in progress; cancel it first")
        return refusals

    def _live_owned_launch_for_deletion(self) -> bool:
        """Whether an owned Cheat Engine is running. A failed probe fails closed.

        Failing closed is right and stays. Failing closed silently is not: the
        user is told a Cheat Engine this plugin owns is still running, which is
        a statement about their machine, when what actually happened is that the
        probe could not answer. Those two produce the same refusal and want
        completely different responses, so the exception is recorded before the
        refusal it turns into.
        """
        try:
            return bool(self.ce_launch.has_live_owned_launch())
        except Exception as exc:  # noqa: BLE001 - an unreadable probe must not permit deletion
            log_failure(
                self.logger, "managed_data.owned_launch_probe_failed", exc, expected=False,
            )
            return True

    def _reset_after_deletion(self, keys: tuple[str, ...]) -> None:
        """Forget what the deleted directories were backing in memory.

        The profile, table, session and config stores re-read their files on
        every access, so they need nothing. The catalog is the exception and is
        quiesced by the caller before the files go; this drops the per-search
        artifact records that would otherwise still resolve to deleted bytes.
        """
        if "cache" in keys:
            self.provider_catalog.artifacts.clear()
            self.provider_catalog.forget_searches()

    def _managed_directories(self) -> list[tuple[str, str, Path, str]]:
        """Every location CE Decky owns, in the order the panel lists them.

        Deletion selects from exactly this list by key, so a scope can never
        name a path the caller supplied and can never drift from what the
        report showed the user before they confirmed.
        """
        return [
            ("tables", "Tables", self.paths.tables_root,
             "Every .CT you downloaded or imported, stored by content hash."),
            ("ce", "Cheat Engine", self.paths.ce_root,
             "The Windows Cheat Engine CE Decky installed or you imported, plus its private runtime copies."),
            ("state", "Profiles and sessions", self.paths.state_root,
             "Per-game choices: selected table, target process, authorization, pinned and remembered cheats, which table sources you switched off, and every table you marked as not working."),
            ("cache", "Provider cache", self.paths.cache_root,
             "Search indexes and provider results. Safe to delete; it is rebuilt on the next search."),
            ("tmp", "Staging", self.paths.temp_root,
             "Work in progress for downloads and installs. Nothing here survives a completed operation."),
            ("settings", "Decky settings", self.paths.settings_dir,
             "The plugin's own configuration, kept by Decky rather than under the managed directory."),
            ("logs", "Logs", self.paths.log_dir,
             "Plugin logs. Deleting them loses diagnostics and nothing else."),
        ]

    def _managed_directory_inventory(self) -> list[dict[str, object]]:
        """What CE Decky has put on disk, directory by directory.

        Removal readiness is what a user consults before uninstalling, so it has
        to answer the question they actually have - what would be deleted, and
        what is in it - rather than only whether deletion is currently allowed.
        Each walk is bounded: this is a report, not a disk audit.
        """
        inventory: list[dict[str, object]] = []
        for key, label, path, purpose in self._managed_directories():
            files, total, truncated, error = _bounded_directory_size(path)
            inventory.append({
                "key": key,
                "label": label,
                "path": str(path),
                "purpose": purpose,
                "exists": path.is_dir(),
                "file_count": files,
                "total_bytes": total,
                "truncated": truncated,
                "error": error,
            })
        return inventory

    def diagnostics_snapshot(self) -> dict[str, object]:
        try:
            tables, table_catalog_errors = self.table_store.list_tables_with_errors()
            table_error = None
        except ValueError as exc:
            tables = []
            table_catalog_errors = []
            table_error = str(exc)[:512]
        try:
            self.config_store.load()
            config_error = None
        except ValueError as exc:
            config_error = str(exc)[:512]
        try:
            profiles = self.profile_store.list_profiles()
            profile_error = None
        except ValueError as exc:
            profiles = []
            profile_error = str(exc)[:512]
        try:
            providers = self.provider_diagnostics.snapshot()["providers"]
            provider_error = None
        except ValueError as exc:
            providers = {}
            provider_error = str(exc)[:512]
        try:
            sessions = self.session_store.inventory()
            session_error = None
        except ValueError as exc:
            sessions = {"apps": [], "total_sessions": 0, "errors": []}
            session_error = str(exc)[:512]
        # The FearLess listing index is the one provider cache with a refresh
        # cycle of its own, and until now it was visible only in a search result.
        fearless_index = self.provider_catalog.fearless_index_status()
        # Which sources this user switched off. Without it every provider
        # counter below reads as a source that answered nothing, and a report
        # about a game that "finds no tables" has no way to say that half the
        # sources were never asked.
        provider_selection = self.provider_sources.snapshot()
        return {
            "uptime_s": max(0, int(time.monotonic() - self._started_monotonic)),
            "log_path": os.environ.get("DECKY_PLUGIN_LOG", ""),
            "version": __version__,
            "storage": {
                "tables": len(tables),
                "table_bytes": sum(int(item.get("size", 0)) for item in tables if isinstance(item.get("size"), int)),
                "profiles": len(profiles),
            },
            "providers": providers,
            "provider_selection": provider_selection,
            "fearless_index": fearless_index,
            "sessions": sessions,
            "config_state_error": config_error,
            "table_state_error": table_error,
            "table_catalog_errors": table_catalog_errors,
            "profile_state_error": profile_error,
            "provider_state_error": provider_error,
            "session_state_error": session_error,
            "capabilities": {
                "provider_offline_core": True,
                "provider_network": True,
                "host_7zip": self._find_7zip() is not None,
            },
            # What the backend's repeating paths have run and cost since load.
            # A report about a hot device or a flat battery is unanswerable from
            # a total for the process: this says which loop was awake.
            "poll_counters": poll_counters.snapshot(),
        }

    def _assert_selectable(self, digest: str, current: str | None) -> None:
        """Refuse a new selection or authorization of a table recorded as not working.

        The mark is the user's own, advisory and reversible, and clearing it
        restores every path it stood in, which is why this refusal names it and
        needs no network to act on. It covers the moment a table is newly taken
        up: a table already selected or already authorized when the mark was
        written keeps both, because withdrawing those is what Revoke is for and
        the user's decision to make. Source and payload conditions are about the
        bytes or the provider rather than about whether the table works, and the
        importer already refuses those where they apply.

        Without this, Search refused the saved copy while Manage offered the same
        bytes one press away, and the two entry points enforced different rules.
        """
        if current is not None and current == digest:
            return
        try:
            record = self.table_blocklist.find(digest)
        except (OSError, ValueError) as exc:
            # Advisory state that cannot be read refuses nothing, here as
            # everywhere else: one damaged file would otherwise make every table
            # on the device unselectable, while the same damage leaves the list
            # that shows and clears these records reading as empty. The user
            # would have no record to point at and no press to lift it with.
            log_activity(self.logger, "warning", "table_blocklist.unreadable",
                         table_sha=digest[:12], reason=str(exc)[:256])
            return
        if is_compatibility_failure(record):
            raise ValueError(
                "this exact table is marked as not working; clear that mark under "
                "Advanced, Tables that did not work, to use it again"
            )

    def save_profile(
        self,
        app_id: int,
        name: str,
        is_shortcut: bool,
        table_sha256: str | None = None,
        target_process: str | None = None,
    ) -> dict[str, object]:
        target_process = _optional_process(target_process)
        with self._mutation_lock:
            existing = self.profile_store.get(app_id)
            if existing is not None and (
                existing.is_shortcut != is_shortcut
                or existing.table_sha256 != table_sha256
                or (existing.target_process or "").casefold() != (target_process or "").casefold()
            ):
                self._assert_no_live_owned_launch(app_id)
            if table_sha256 is not None:
                digest = self._sha(table_sha256)
                self.table_store.verified_blob(digest)
                self._assert_selectable(digest, None if existing is None else existing.table_sha256)
            else:
                digest = None
            profile = self.profile_store.upsert(
                app_id=app_id,
                name=name,
                is_shortcut=is_shortcut,
                table_sha256=digest,
                target_process=target_process,
            )
            log_activity(
                self.logger, "info", "profile.saved",
                app_id=app_id, shortcut=is_shortcut,
                table_sha=None if digest is None else digest[:12],
                target=target_process,
                changed_table=existing is None or existing.table_sha256 != digest,
                changed_target=existing is None or (existing.target_process or "").casefold() != (target_process or "").casefold(),
            )
            return profile.as_dict()

    def delete_profile(self, app_id: int) -> dict[str, object]:
        with self._mutation_lock:
            self._assert_no_live_owned_launch(app_id)
            if self.session_store.load_current(app_id) is not None:
                raise ValueError("cannot delete a profile while a prepared session is current; retire the session first")
            deleted = self.profile_store.delete(app_id)
            log_activity(self.logger, "info", "profile.deleted", app_id=app_id, deleted=deleted)
            self._runtime_journal.forget(app_id)
            return {"deleted": deleted}

    def set_execution_consent(self, app_id: int, table_sha256: str, consent: bool) -> dict[str, object]:
        with self._mutation_lock:
            digest = self._sha(table_sha256)
            profile = self.profile_store.get(app_id)
            if profile is not None and (not consent or profile.execution_consent_sha256 != digest):
                self._assert_no_live_owned_launch(app_id)
            if consent is True:
                self.table_store.verified_blob(digest)
                self._assert_selectable(digest, None if profile is None else profile.execution_consent_sha256)
            profile = self.profile_store.set_execution_consent(
                app_id=app_id,
                table_sha256=digest,
                consent=consent,
            )
            log_activity(
                self.logger, "info", "profile.consent_set",
                app_id=app_id, table_sha=digest[:12], consent=consent,
            )
            return profile.as_dict()

    def revoke_table(self, app_id: int, table_sha256: str) -> dict[str, object]:
        """Detach a durable profile, including one whose game was removed."""
        with self._mutation_lock:
            digest = self._sha(table_sha256)
            profile = self.profile_store.get(app_id)
            if profile is None or profile.table_sha256 not in (None, digest):
                raise ValueError("selected table changed; refresh before revoking it")
            self._assert_no_live_owned_launch(app_id)
            current = self.session_store.load_current(app_id)
            if current is not None:
                if current.table_sha256 != digest:
                    raise ValueError("prepared table changed; refresh before revoking it")
                self.retire_session(app_id, current.session_id)
            profile = self.profile_store.revoke_table(app_id=app_id, table_sha256=digest)
            log_activity(self.logger, "info", "profile.table_revoked", app_id=app_id, table_sha=digest[:12])
            return profile.as_dict()

    def set_startup_preference(
        self,
        app_id: int,
        table_sha256: str,
        record_id: int,
        active: bool | None = None,
        value: str | None = None,
    ) -> dict[str, object]:
        with self._mutation_lock:
            digest = self._sha(table_sha256)
            inspection = self._inspect_table_blob(self.table_store.verified_blob(digest), digest, app_id)
            control = self._actionable_control_by_id(inspection.controls, record_id)
            if value is not None:
                if control.kind not in {"value", "dropdown"}:
                    raise ValueError("startup value can only be assigned to value/dropdown MemoryRecords")
                if control.dropdown_read_only:
                    allowed = {item[0] for item in control.dropdown_values}
                    if value not in allowed:
                        raise ValueError("startup value is outside the exact table's read-only dropdown")
            # Remembered and configured writes are budgeted against the exact
            # expanded plan; this legacy path was still counting stored fields
            # only, so the implicit enclosing scripts a startup preference needs
            # could push the descriptor past its ceiling after the profile had
            # already accepted the write.
            existing = self._require_profile(app_id, digest)
            prospective = {item.record_id: item for item in existing.startup}
            prospective[record_id] = StartupPreference(record_id, active, value)
            # Budgeted with auto-load treated as on, the same worst case the
            # remembered and configured writes use: the ceiling has to hold for
            # the plan this profile could produce, not only the one it produces
            # while auto-load happens to be off.
            self._assert_effective_startup_plan_fits(
                replace(
                    existing,
                    autoload_enabled=True,
                    startup=[prospective[key] for key in sorted(prospective)],
                ),
                inspection,
            )
            profile = self.profile_store.set_startup(
                app_id=app_id,
                table_sha256=digest,
                record_id=record_id,
                active=active,
                value=value,
            )
            return profile.as_dict()

    def _require_profile(self, app_id: int, digest: str):
        profile = self.profile_store.get(app_id)
        if profile is None:
            raise ValueError("game profile does not exist")
        if profile.table_sha256 != digest:
            raise ValueError("this operation is bound to the profile's exact selected table SHA")
        return profile

    def clear_startup_preference(self, app_id: int, table_sha256: str, record_id: int | None = None) -> dict[str, object]:
        with self._mutation_lock:
            digest = self._sha(table_sha256)
            self.table_store.verified_blob(digest)
            profile = self.profile_store.clear_startup(app_id=app_id, table_sha256=digest, record_id=record_id)
            return profile.as_dict()

    def associate_table(self, app_id: int, table_sha256: str) -> dict[str, object]:
        with self._mutation_lock:
            digest = self._sha(table_sha256)
            self.table_store.verified_blob(digest)
            existing = self.profile_store.get(app_id)
            self._assert_selectable(digest, None if existing is None else existing.table_sha256)
            profile = self.profile_store.associate_table(app_id=app_id, table_sha256=digest)
            log_activity(
                self.logger, "info", "profile.table_associated",
                app_id=app_id, table_sha=digest[:12], library=len(profile.table_library),
            )
            return profile.as_dict()

    def set_autoload(self, app_id: int, table_sha256: str, enabled: bool) -> dict[str, object]:
        with self._mutation_lock:
            digest = self._sha(table_sha256)
            # Arming automatic execution requires the exact table to be present
            # and verified. Disarming reduces authority, so requiring the same
            # proof only meant that a deleted or corrupted blob left the durable
            # bit armed with no way for the user to clear it - and it became
            # executable again the moment the table was repaired.
            if enabled:
                self.table_store.verified_blob(digest)
            profile = self.profile_store.set_autoload(app_id=app_id, table_sha256=digest, enabled=enabled)
            log_activity(
                self.logger, "info", "profile.autoload_set",
                app_id=app_id, table_sha=digest[:12], enabled=enabled,
            )
            return profile.as_dict()

    def set_remembered_cheats(self, app_id: int, table_sha256: str, states: list[dict[str, object]]) -> dict[str, object]:
        with self._mutation_lock:
            digest = self._sha(table_sha256)
            if not isinstance(states, list) or len(states) > 1024:
                raise ValueError("remembered cheat state must be a bounded list")
            inspection = self._inspect_table_blob(self.table_store.verified_blob(digest), digest, app_id)
            parsed: list[StartupPreference] = []
            seen: set[int] = set()
            for raw in states:
                if not isinstance(raw, dict) or set(raw) != {"record_id", "active", "value"}:
                    raise ValueError("remembered cheat state entry is malformed")
                record_id = raw.get("record_id")
                active = raw.get("active")
                value = raw.get("value")
                if isinstance(record_id, bool) or not isinstance(record_id, int):
                    raise ValueError("remembered cheat record_id must be an integer")
                if record_id in seen:
                    raise ValueError("remembered cheat state contains duplicate MemoryRecord ID")
                seen.add(record_id)
                if active is not None and not isinstance(active, bool):
                    raise ValueError("remembered cheat active state must be boolean or null")
                if value is not None and not isinstance(value, str):
                    raise ValueError("remembered cheat value must be string or null")
                if active is None and value is None:
                    raise ValueError("remembered cheat state entry must contain active and/or value")
                control = self._actionable_control_by_id(inspection.controls, record_id)
                if value is not None:
                    if control.kind not in {"value", "dropdown"}:
                        raise ValueError("remembered value can only be assigned to value/dropdown MemoryRecords")
                    if control.dropdown_read_only:
                        allowed = {item[0] for item in control.dropdown_values}
                        if value not in allowed:
                            raise ValueError("remembered value is outside the exact table's read-only dropdown")
                parsed.append(StartupPreference(record_id, active, value))
            self._assert_effective_startup_plan_fits(
                replace(self._require_profile(app_id, digest), autoload_enabled=True, remembered=parsed),
                inspection,
            )
            profile = self.profile_store.set_remembered(app_id=app_id, table_sha256=digest, states=parsed)
            return profile.as_dict()

    def set_configured_values(self, app_id: int, table_sha256: str, values: list[dict[str, object]]) -> dict[str, object]:
        with self._mutation_lock:
            digest = self._sha(table_sha256)
            if not isinstance(values, list) or len(values) > 1024:
                raise ValueError("configured values must be a bounded list")
            inspection = self._inspect_table_blob(self.table_store.verified_blob(digest), digest, app_id)
            parsed: list[ConfiguredValue] = []
            seen: set[int] = set()
            for raw in values:
                if not isinstance(raw, dict) or set(raw) != {"record_id", "value"}:
                    raise ValueError("configured value entry is malformed")
                record_id = raw.get("record_id")
                value = raw.get("value")
                if isinstance(record_id, bool) or not isinstance(record_id, int):
                    raise ValueError("configured value record_id must be an integer")
                if record_id in seen:
                    raise ValueError("configured values contain duplicate MemoryRecord ID")
                seen.add(record_id)
                if not isinstance(value, str):
                    raise ValueError("configured value must be a string")
                if not is_configurable_value(value):
                    raise ValueError("configured value must not be blank or Cheat Engine's unreadable placeholder")
                control = self._actionable_control_by_id(inspection.controls, record_id)
                if control.kind not in {"value", "dropdown"}:
                    raise ValueError("configured value can only target value/dropdown MemoryRecords")
                if control.dropdown_read_only:
                    allowed = {item[0] for item in control.dropdown_values}
                    if value not in allowed:
                        raise ValueError("configured value is outside the exact table's read-only dropdown")
                parsed.append(ConfiguredValue(record_id, value))
            self._assert_effective_startup_plan_fits(
                replace(self._require_profile(app_id, digest), autoload_enabled=True, configured_values=parsed),
                inspection,
            )
            profile = self.profile_store.set_configured_values(
                app_id=app_id, table_sha256=digest, values=parsed
            )
            return profile.as_dict()

    def _assert_effective_startup_plan_fits(self, profile, inspection) -> None:
        """Reject durable state whose expanded startup plan could never launch.

        The store can only count the fields it holds; the descriptor is built
        from an expansion of them that adds the enclosing scripts a remembered
        record requires. Checking only the stored fields let a profile be
        accepted as within budget and then fail every session preparation, so
        the exact expanded count is checked here, where the table is known.
        """
        actions = effective_startup_plan(profile, inspection)
        if len(actions) > MAX_STARTUP_ACTIONS:
            raise ValueError(
                f"this selection expands to {len(actions)} startup actions for the exact table; "
                f"the safe per-session limit is {MAX_STARTUP_ACTIONS}. Reduce the remembered or "
                "configured selection before applying."
            )

    def validate_effective_startup_plan(
        self,
        app_id: int,
        table_sha256: str,
        remembered: list[dict[str, object]] | None = None,
        configured_values: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        """Report the exact expanded startup plan a prospective selection would create.

        The panel used to preflight with its own count of the fields it was
        about to write, which omitted the configured values already persisted
        and every implicit enclosing script. A selection could therefore mutate
        the running game and only then be refused persistence. This answers the
        same question the backend will ask, before the first runtime command.
        """
        with self._mutation_lock:
            digest = self._sha(table_sha256)
            inspection = self._inspect_table_blob(self.table_store.verified_blob(digest), digest, app_id)
            profile = self.profile_store.get(app_id)
            if profile is None:
                raise ValueError("game profile does not exist")
            if profile.table_sha256 != digest:
                raise ValueError("startup plan validation is bound to the profile's exact selected table SHA")
            prospective = replace(
                profile,
                autoload_enabled=True,
                remembered=(
                    profile.remembered if remembered is None
                    else [self._startup_preference(item) for item in self._bounded_list(remembered, "remembered cheat state")]
                ),
                configured_values=(
                    profile.configured_values if configured_values is None
                    else [self._configured_value(item) for item in self._bounded_list(configured_values, "configured values")]
                ),
            )
            actions = effective_startup_plan(prospective, inspection)
            return {
                "action_count": len(actions),
                "limit": MAX_STARTUP_ACTIONS,
                "fits": len(actions) <= MAX_STARTUP_ACTIONS,
            }

    @staticmethod
    def _bounded_list(value: object, label: str) -> list[dict[str, object]]:
        if not isinstance(value, list) or len(value) > 1024:
            raise ValueError(f"{label} must be a bounded list")
        return value

    @staticmethod
    def _startup_preference(raw: object) -> StartupPreference:
        if not isinstance(raw, dict) or set(raw) != {"record_id", "active", "value"}:
            raise ValueError("remembered cheat state entry is malformed")
        record_id = raw.get("record_id")
        active = raw.get("active")
        value = raw.get("value")
        if isinstance(record_id, bool) or not isinstance(record_id, int):
            raise ValueError("remembered cheat record_id must be an integer")
        if active is not None and not isinstance(active, bool):
            raise ValueError("remembered cheat active state must be boolean or null")
        if value is not None and not isinstance(value, str):
            raise ValueError("remembered cheat value must be string or null")
        return StartupPreference(record_id, active, value)

    @staticmethod
    def _configured_value(raw: object) -> ConfiguredValue:
        if not isinstance(raw, dict) or set(raw) != {"record_id", "value"}:
            raise ValueError("configured value entry is malformed")
        record_id = raw.get("record_id")
        value = raw.get("value")
        if isinstance(record_id, bool) or not isinstance(record_id, int):
            raise ValueError("configured value record_id must be an integer")
        if not isinstance(value, str):
            raise ValueError("configured value must be a string")
        return ConfiguredValue(record_id, value)

    def set_pinned_control(self, app_id: int, table_sha256: str, record_id: int, pinned: bool) -> dict[str, object]:
        with self._mutation_lock:
            digest = self._sha(table_sha256)
            inspection = self._inspect_table_blob(self.table_store.verified_blob(digest), digest, app_id)
            self._actionable_control_by_id(inspection.controls, record_id)
            profile = self.profile_store.set_pinned(
                app_id=app_id, table_sha256=digest, record_id=record_id, pinned=pinned
            )
            return profile.as_dict()

    def clear_pinned_controls(self, app_id: int, table_sha256: str) -> dict[str, object]:
        with self._mutation_lock:
            digest = self._sha(table_sha256)
            self.table_store.verified_blob(digest)
            profile = self.profile_store.clear_pinned(app_id=app_id, table_sha256=digest)
            return profile.as_dict()

    def prepare_session(self, app_id: int) -> dict[str, object]:
        with self._mutation_lock:
            self._assert_no_managed_ce_transition()
            self._assert_session_replacement_safe(app_id)
            profile = self.profile_store.get(app_id)
            if profile is None:
                raise ValueError("game profile does not exist")
            if profile.table_sha256 is None:
                raise ValueError("game profile has no selected table")
            digest = self._sha(profile.table_sha256)
            # Consent used to be enforced only by the staleness of an already
            # prepared session, which meant the check disappeared the moment a
            # launch was allowed to replace a stale one. Preparation is the
            # execution boundary, so it states the requirement itself.
            if profile.execution_consent_sha256 != digest:
                raise ValueError("this exact table has no execution consent; review and authorize it first")
            blob = self.table_store.verified_blob(digest)
            inspection = self._inspect_table_blob(blob, digest, app_id)
            imported = self._validated_ce_import()
            self._assert_session_replacement_safe(app_id)
            prepared = self.session_store.prepare(profile, blob, inspection, imported.sha256)
            actions = effective_startup_plan(profile, inspection)
            # The same eligibility the descriptor carries to the bridge, so the
            # record the bridge reports and the records accepted here cannot
            # disagree about what a successful startup proved.
            leaves = {action.record_id for action in actions if action.proof}
            self._startup_compatibility_pending.pop(app_id, None)
            if leaves:
                try:
                    floor, epochs = self.table_blocklist.failure_epochs()
                    self._startup_compatibility_pending[app_id] = {
                        "session": prepared.session_id, "digest": digest, "leaves": leaves,
                        "total": len(actions), "epoch": epochs.get(digest, floor),
                    }
                    while len(self._startup_compatibility_pending) > MAX_STARTUP_ACTIONS:
                        self._startup_compatibility_pending.pop(next(iter(self._startup_compatibility_pending)))
                except (OSError, ValueError) as exc:
                    log_activity(self.logger, "warning", "table_compatibility.startup_unavailable", app_id=app_id, reason=str(exc)[:256])
            log_activity(
                self.logger, "info", "session.prepared",
                app_id=app_id, session=prepared.session_id[:12], table_sha=digest[:12],
            )
            return prepared.as_dict()

    def get_runtime_status(self, app_id: int) -> dict[str, object]:
        envelope = self._read_runtime_status(app_id)
        self._journal_runtime_state(app_id, envelope)
        try:
            self._observe_compatibility(app_id, envelope)
        except (OSError, ValueError) as exc:
            log_activity(self.logger, "warning", "table_compatibility.not_recorded", app_id=app_id, reason=str(exc)[:256])
        return envelope

    def _journal_runtime_state(self, app_id: int, envelope: dict[str, object]) -> None:
        """Record what the resident bridge reports, once per change.

        This is the evidence for every "the cheat did nothing" report: whether
        Cheat Engine was attached, to which PID, whether it had the table, and
        what the last command it ran said. It used to exist only in the panel,
        which is to say only until the panel was closed.
        """
        status = envelope.get("status")
        status = status if isinstance(status, dict) else {}
        prepared = envelope.get("prepared")
        session = str(prepared.get("session_id", ""))[:12] if isinstance(prepared, dict) else ""
        results = status.get("results")
        results = results if isinstance(results, list) else []
        failed = next(
            (item for item in reversed(results) if isinstance(item, dict) and not item.get("ok")),
            None,
        )
        fields: dict[str, object] = {
            "app_id": app_id,
            "connected": bool(envelope.get("connected")),
            "session_current": bool(envelope.get("session_current")),
            "session": session or None,
            "attached": status.get("attached"),
            "opened_pid": status.get("opened_process_id"),
            "target": status.get("target_process"),
            "table_load": status.get("table_load_state"),
            # Whether the table's own Lua script was authorized without Cheat
            # Engine asking, which is the exact-SHA consent from Review being
            # carried into the load, or whether the older path form was used and
            # Cheat Engine was still free to ask. A table that ran code is a
            # thing a bug report has to be able to establish afterwards.
            "table_load_route": status.get("table_load_route"),
            "table_load_error": status.get("table_load_error"),
            "bridge_phase": status.get("bridge_phase"),
            "startup_state": status.get("startup_state"),
            "startup_progress": None if status.get("startup_total") is None
            else f"{status.get('startup_completed')}/{status.get('startup_total')}",
            "address_list": status.get("address_list_count"),
            "window_suppressions": status.get("window_suppressions"),
            # A window the sweep could not put down is on screen over the game
            # until something takes it down, which is the worst outcome this
            # sweep exists to prevent and the one a bug report cannot infer. The
            # total says how hard it tried; the flag says whether the game is
            # covered now, and is the half that can go back to no.
            "unsuppressed_sweeps": status.get("unsuppressed_sweeps"),
            "window_over_game": status.get("window_over_game"),
            "dialogs_dismissed": status.get("dialogs_dismissed"),
            "last_dialog": status.get("last_dialog"),
            "minimized_query": status.get("minimized_query"),
            # Asking a pushed-aside game back and telling it that it is active
            # again are two different halves, and a game left running at a
            # fraction of its frame rate is missing the second one. A bug report
            # about that cannot be answered without both counts.
            "game_restores": status.get("game_restores"),
            "game_activations": status.get("game_activations"),
            "startup_active_ids": ",".join(str(record_id) for record_id in status.get("startup_active_ids", ())),
            "focus_discovery_attempts": status.get("focus_discovery_attempts"),
            "focus_capability": status.get("focus_capability"),
            "focus_error": status.get("focus_error"),
            "focus_candidates": status.get("focus_candidates"),
            "focus_attempts": status.get("focus_attempts"),
            "focus_successes": status.get("focus_successes"),
            "focus_reason": status.get("focus_reason"),
            "restore_capability": status.get("restore_capability"),
            "restore_error": status.get("restore_error"),
            "stale_reason": envelope.get("session_stale_reason"),
            "state_reason": envelope.get("session_state_reason"),
            "terminal_reason": envelope.get("terminal_reason"),
            "status_unreadable": bool(envelope.get("status_unreadable")),
            "processes": len(status.get("processes") or ()),
            "last_failure_record": None if failed is None else failed.get("record_id"),
            "last_failure": None if failed is None else failed.get("error"),
            "last_failure_code": None if failed is None else failed.get("error_code"),
        }
        # Startup progress and the process count move while nothing meaningful
        # changed, so they are reported without being part of the signature.
        signature = tuple(
            fields[name] for name in (
                "connected", "session_current", "session", "attached", "opened_pid", "target",
                "table_load", "table_load_route", "table_load_error", "bridge_phase", "startup_state", "window_suppressions", "unsuppressed_sweeps", "window_over_game",
                "dialogs_dismissed", "last_dialog", "minimized_query", "game_restores", "game_activations",
                "focus_capability", "focus_error", "startup_active_ids", "focus_discovery_attempts", "focus_candidates", "focus_attempts", "focus_successes", "focus_reason",
                "restore_capability", "restore_error", "stale_reason",
                "state_reason", "terminal_reason", "status_unreadable",
                "last_failure_record", "last_failure", "last_failure_code",
            )
        )
        self._runtime_journal.observe(app_id, signature, **fields)

    def _read_runtime_status(self, app_id: int) -> dict[str, object]:
        try:
            prepared = self.session_store.load_current(app_id)
            session_state_reason: str | None = None
        except ValueError as exc:
            # Strict parsing is correct, but raising here made every recovery
            # path for this game inaccessible: preparation, retirement and
            # profile deletion all begin from this read. Report the exact
            # condition so the controller can offer the authorized repair.
            prepared = None
            session_state_reason = str(exc)[:512]
        if prepared is None:
            if session_state_reason is not None:
                return {
                    "prepared": None,
                    "status": None,
                    "next_generation": 1,
                    "status_age_ms": None,
                    "status_fresh": False,
                    "status_clock_skew": False,
                    "status_unreadable": False,
                    "session_current": False,
                    "session_stale_reason": session_state_reason,
                    "session_state_reason": session_state_reason,
                    "connected": False,
                }
            return {
                "prepared": None,
                "status": None,
                "next_generation": 1,
                "status_age_ms": None,
                "status_fresh": False,
                "status_clock_skew": False,
                "status_unreadable": False,
                "session_current": False,
                "session_stale_reason": "no prepared session exists for AppID",
                "session_state_reason": None,
                "connected": False,
            }
        session_stale_reason = self._session_stale_reason(prepared)
        session_current = session_stale_reason is None
        try:
            status, status_mtime_ns = self.session_store.read_status_observation(prepared)
            next_generation: int | None = self.session_store.next_generation(prepared)
        except ValueError as exc:
            # Corruption of the mutable control/status protocol is not corruption
            # of the immutable session: the pointer, metadata and descriptor all
            # still read, so `repair_session_state()` refused the repair while
            # every path that needs runtime status raised out of this call. Report
            # it as its own repairable state instead, and never publish a
            # generation derived from a control log this build cannot parse.
            reason = _runtime_protocol_reason(exc)
            return {
                "prepared": prepared.as_dict(),
                "status": None,
                "next_generation": None,
                "status_age_ms": None,
                "status_fresh": False,
                "status_clock_skew": False,
                "status_unreadable": True,
                "session_current": False,
                "session_stale_reason": session_stale_reason if session_stale_reason is not None else reason,
                "session_state_reason": reason,
                "connected": False,
                "terminal_reason": None,
            }
        status_age_ms: int | None = None
        status_fresh = False
        status_clock_skew = False
        if status is not None and status_mtime_ns is not None:
            delta_s = time.time() - (status_mtime_ns / 1_000_000_000)
            if delta_s < -RUNTIME_STATUS_MAX_FUTURE_SKEW_SECONDS:
                status_clock_skew = True
                status_age_ms = 0
            else:
                status_age_ms = max(0, int(delta_s * 1000))
                status_fresh = status_age_ms <= int(RUNTIME_HEARTBEAT_TTL_SECONDS * 1000)
        terminal_reason: str | None = None
        exited_at = self.ce_launch.confirmed_exit_epoch(prepared.app_id, prepared.session_id)
        if exited_at is not None and (status_mtime_ns is None or (status_mtime_ns / 1_000_000_000) <= exited_at):
            # A heartbeat written before the owned group was proven gone cannot
            # prove a connected current bridge, even inside its ordinary TTL.
            status_fresh = False
            status_clock_skew = False
            terminal_reason = "owned_bridge_process_exited"
        return {
            "prepared": prepared.as_dict(),
            "status": None if status is None else asdict(status),
            "next_generation": next_generation,
            "status_age_ms": status_age_ms,
            "status_fresh": status_fresh,
            "status_clock_skew": status_clock_skew,
            "status_unreadable": False,
            "session_current": session_current,
            "session_stale_reason": session_stale_reason,
            "session_state_reason": None,
            "connected": bool(status is not None and status_fresh and not status_clock_skew and session_current),
            "terminal_reason": terminal_reason,
        }

    def retire_session(self, app_id: int, expected_session_id: str) -> dict[str, object]:
        """Stop treating one exact prepared session as current without deleting its evidence."""
        with self._mutation_lock:
            runtime = self.get_runtime_status(app_id)
            prepared = runtime.get("prepared")
            if prepared is None:
                return {"retired": False, "session_id": None, "requires_target_validation": True}
            current_session_id = prepared.get("session_id") if isinstance(prepared, dict) else None
            if not isinstance(expected_session_id, str) or expected_session_id != current_session_id:
                raise ValueError("current prepared session changed; refresh before retiring it")
            current = self.session_store.load_current(app_id)
            if current is None or current.session_id != expected_session_id:
                raise ValueError("current prepared session changed; refresh before retiring it")
            if self._bridge_blocks_session_change(runtime, current):
                raise ValueError("cannot retire a session while the resident bridge has a fresh heartbeat or a clock-skewed heartbeat")
            runtime = self.get_runtime_status(app_id)
            if runtime.get("prepared") is None or runtime["prepared"].get("session_id") != expected_session_id:
                raise ValueError("current prepared session changed; refresh before retiring it")
            if self._bridge_blocks_session_change(runtime, current):
                raise ValueError("cannot retire a session while the resident bridge has a fresh heartbeat or a clock-skewed heartbeat")
            pointer = self.session_store.root / str(current.app_id) / "current.json"
            if pointer.is_symlink() or not pointer.is_file():
                raise ValueError("current session pointer is not a regular file")
            durable_unlink(pointer)
            log_activity(
                self.logger, "info", "session.retired",
                app_id=app_id, session=expected_session_id[:12],
            )
            return {"retired": True, "session_id": expected_session_id, "requires_target_validation": True}

    def repair_session_state(self, app_id: int) -> dict[str, object]:
        """Discard a current-session pointer this build cannot read.

        Only reachable when the ordinary read has actually failed, and only
        after proving that no Cheat Engine CE Decky owns can still be referring
        to that session - for this game and at launcher scope, because an
        ambiguous owner is not a safe basis for discarding session identity.

        The session directory itself is kept: it is the evidence for whatever
        corrupted it.
        """
        with self._mutation_lock:
            try:
                current = self.session_store.load_current(app_id)
            except ValueError:
                pass
            else:
                # A readable pointer is not proof the session is usable: the
                # mutable control/status protocol is a separate failure domain,
                # and corruption there blocked runtime status, retirement and
                # replacement alike with no controller route out.
                if current is None or self._runtime_protocol_state_reason(current) is None:
                    raise ValueError("this game's current session is readable; retire it normally instead")
            self._assert_no_live_owned_launch(app_id)
            if self.ce_launch.has_live_owned_launch():
                raise ValueError(
                    "a Cheat Engine CE Decky owns may still be running; stop it before discarding session state"
                )
            outcome = self.session_store.discard_current_pointer(app_id)
            log_activity(self.logger, "info", "session.repaired", app_id=app_id, **outcome)
            return {**outcome, "app_id": app_id}

    def repair_profile_state(self) -> dict[str, object]:
        """Quarantine an unreadable profile store and start a clean one.

        `get_status()` already degrades a corrupt `profiles.json` to an empty
        list so the panel stays alive, but every per-game write re-opened the
        same file and failed, so no game could be configured again from Game
        Mode. This is the authorized repair: nothing owned may still depend on
        the identities the unreadable file holds, and the file itself is kept
        as the evidence for whatever corrupted it.
        """
        with self._mutation_lock:
            self._assert_no_managed_ce_transition()
            if self.ce_launch.has_live_owned_launch():
                raise ValueError(
                    "a Cheat Engine CE Decky owns may still be running; stop it before discarding profile state"
                )
            outcome = self.profile_store.quarantine_corrupt_state()
            log_activity(self.logger, "info", "profiles.repaired", quarantined=str(outcome.get("quarantined")))
            return outcome

    def repair_owned_launch_state(self, app_id: int) -> dict[str, object]:
        """Quarantine unreadable launch ownership after a global absence proof."""
        with self._mutation_lock:
            outcome = self.ce_launch.repair_invalid_owned_launch_record(app_id)
            log_activity(self.logger, "info", "ce_launch.ownership_repaired", **outcome)
            return outcome

    def write_runtime_commands(self, app_id: int, commands: list[dict[str, object]]) -> dict[str, object]:
        with self._mutation_lock:
            if not isinstance(commands, list):
                raise ValueError("commands must be a list")
            parsed = [self._runtime_command(item) for item in commands]
            prepared = self.session_store.load_current(app_id)
            if prepared is None:
                raise ValueError("no prepared session exists for AppID")
            stale_reason = self._session_stale_reason(prepared)
            if stale_reason is not None:
                raise ValueError(f"prepared session is stale; prepare a new session: {stale_reason}")
            # Runtime authorization is a host boundary, so the compatibility of
            # the resident bridge belongs here rather than only in the panel that
            # renders it. Decky is known to retain overlapping frontend observers
            # across a reload, so an older frontend can reach a freshly loaded
            # backend without the guard the new one has - and would then send
            # current commands to the previous resident protocol.
            mismatch = self._recovered_bridge_mismatch(
                self.ce_launch.recover_owned_launch(app_id),
                expected_ce_sha256=prepared.ce_sha256,
            )
            if mismatch is not None:
                raise ValueError(
                    f"{mismatch}; stop Cheat Engine and start it again before controlling cheats"
                )
            blob = self.table_store.verified_blob(prepared.table_sha256)
            inspection = self._inspect_table_blob(blob, prepared.table_sha256, app_id)
            for command in parsed:
                if command.kind not in {"query", "set_active", "set_value"}:
                    continue
                assert command.record_id is not None
                control = self._actionable_control_by_id(inspection.controls, command.record_id)
                if command.kind == "set_value":
                    if control.kind not in {"value", "dropdown"}:
                        raise ValueError("set_value can only target value/dropdown MemoryRecords")
                    if control.dropdown_read_only:
                        allowed = {item[0] for item in control.dropdown_values}
                        if command.value not in allowed:
                            raise ValueError("set_value is outside the exact table's read-only dropdown")
            runtime = self.get_runtime_status(app_id)
            protocol_reason = runtime.get("session_state_reason")
            if bool(runtime.get("status_unreadable")) and isinstance(protocol_reason, str):
                raise ValueError(f"{protocol_reason}; repair this game's session state before controlling cheats")
            if not bool(runtime.get("connected")):
                raise ValueError(_disconnected_bridge_reason(runtime))
            if any(command.kind in {"query", "set_active", "set_value"} for command in parsed):
                status = runtime.get("status")
                attached = isinstance(status, dict) and status.get("attached") is True
                opened_process_id = status.get("opened_process_id") if isinstance(status, dict) else None
                if not attached or isinstance(opened_process_id, bool) or not isinstance(opened_process_id, int) or opened_process_id <= 0:
                    raise ValueError("resident bridge is connected but not attached to a target process")
            # The control file is published by `os.replace` inside this call, so
            # a failure it raises is either a refusal before that point or the
            # typed `DurabilityUnknownError` from after it, which the frontend
            # reconciles instead of reporting as a definite failure.
            next_generation = self.session_store.write_commands(prepared, parsed)
            pending = self._compatibility_pending.setdefault(app_id, {"session": prepared.session_id, "records": {}})
            if pending["session"] != prepared.session_id:
                pending = {"session": prepared.session_id, "records": {}}
                self._compatibility_pending[app_id] = pending
            for command in parsed:
                if command.kind == "set_active":
                    pending["records"].pop(command.record_id, None)
                    if command.value == "1":
                        pending["records"][command.record_id] = {"activation": command.generation, "acknowledged": False, "query": None}
                elif command.kind == "query" and command.record_id in pending["records"]:
                    pending["records"][command.record_id]["query"] = command.generation
                    pending["records"][command.record_id]["verified"] = False
            if not pending["records"] or len(pending["records"]) > MAX_STARTUP_ACTIONS:
                self._compatibility_pending.pop(app_id, None)

            # Past this point the batch is durably accepted, so nothing may
            # raise: an ordinary exception after the commit reaches the caller
            # as a rejection and would describe commands Cheat Engine is
            # already free to execute as never having been written.
            try:
                # Which cheats were switched, and to what. "The toggle did
                # nothing" is answerable only by pairing this against the
                # results the bridge reports back in `runtime.state_changed`.
                log_activity(
                    self.logger, "info", "runtime.commands_written",
                    app_id=app_id, session=prepared.session_id[:12],
                    count=len(parsed), next_generation=next_generation,
                    commands=";".join(
                        f"{command.kind}"
                        + (f":{command.record_id}" if command.record_id is not None else "")
                        + (f"={command.value}" if command.value is not None else "")
                        for command in parsed[:24]
                    ) or None,
                )
            except Exception:  # noqa: BLE001 - a lost log line must not rewrite a committed result
                pass
            return {"ok": True, "count": len(parsed), "next_generation": next_generation}

    def prepare_private_ce_runtime(self) -> dict[str, object]:
        with self._mutation_lock:
            self._assert_no_managed_ce_transition()
            imported = self._validated_ce_import()
            # Cheat Engine writes into its own installation directory, so the
            # promoted runtime snapshot stops matching its manifest after the
            # first run. Rebuilding it is safe only while nothing is executing
            # out of that shared tree.
            runtime = materialize_private_runtime(
                source_root=Path(imported.root),
                source_executable=Path(imported.executable),
                source_executable_sha256=imported.sha256,
                managed_ce_root=self.paths.ce_root,
                bridge_source=self.bridge_source,
                allow_replace=not self.ce_launch.has_live_owned_launch(),
            )
            log_activity(
                self.logger, "info", "runtime.materialized",
                executable_sha=runtime.source_executable_sha256[:12],
                tree_sha=runtime.source_tree_sha256[:12],
                bridge_sha=runtime.bridge_sha256[:12],
            )
            # A changed bridge or reinstalled Cheat Engine produces a new runtime
            # key beside the old one, and each is close to a full CE tree. Collect
            # the superseded ones only while nothing CE Decky owns can still be
            # executing out of any of them - the same proof that allows replacing
            # a dirtied tree.
            if not self.ce_launch.has_live_owned_launch():
                collected = collect_stale_runtimes(self.paths.ce_root, Path(runtime.root).name)
                if collected:
                    log_activity(self.logger, "info", "runtime.stale_collected", count=len(collected))
            return runtime.as_dict()

    # -- Plugin-owned Cheat Engine launch ---------------------------------

    def list_running_app_ids(self) -> dict[str, object]:
        """Offer running-game candidates when Steam exposes no such query.

        Steam build 1785799196 has no ``SteamClient.GameSessions.GetRunningApps``,
        so the frontend cannot observe running games at all and every user must
        pick the game by hand. The bounded process-table scan only proposes
        AppIDs Steam itself declared; the caller still matches them against the
        Steam library, and every launch path resolves prefix and Proton identity
        independently of this observation.
        """
        return observe_running_app_ids().as_dict()

    def local_library(self) -> dict[str, object]:
        """Which of the account's library entries this device actually holds.

        Steam's library belongs to an account and the panel was offering all of
        it: on a second device most of those titles are files on the first one,
        and choosing one is a game this plugin can do nothing with. The device's
        own manifests and its own shortcut store are what say otherwise, and
        what Steam calls a tool rather than a game comes with them, because
        Proton and the Steam Linux Runtimes are installed apps nobody wants
        offered as something to cheat in.

        A read that fails is reported as a reason rather than as an empty
        device: an empty answer would hide the user's whole library.
        """
        try:
            found = local_library(self.paths.user_home)
        except (OSError, ValueError, RuntimeError) as exc:
            log_activity(
                self.logger, "warning", "steam_library.unreadable", reason=str(exc)[:256],
            )
            return {
                "schema": 1, "steam_app_ids": [], "unstartable_app_ids": [], "shortcut_app_ids": [],
                "reason": "this device's Steam library could not be read",
                "shortcuts_reason": "this device's Steam library could not be read",
            }
        log_activity(
            self.logger, "info", "steam_library.listed",
            installed=len(found["steam_app_ids"]), unstartable=len(found["unstartable_app_ids"]),
            shortcuts=len(found["shortcut_app_ids"]),
            reason=found["reason"], shortcuts_reason=found["shortcuts_reason"],
        )
        return found

    def list_game_executables(self, app_id: int) -> dict[str, object]:
        """What this installed game starts, for a table that names no process.

        Most tables name no process, and a Steam library entry's launch
        executable exists only in the running game's process table, so a table
        downloaded for a game that is not running had no candidate at all and
        could not be authorized without starting the game first. Steam's own
        application cache answers it without the game running: the client has to
        know what to start, and that record names the exact program, for the
        branch this device has installed. Where it says nothing, the game's own
        installed executables are listed instead, read only, resolved through
        Steam's own manifest under the same library discovery every other Steam
        read here uses.

        It is evidence and not a decision. Nothing here claims which executable
        owns the game's memory; the frontend ranks it below every stronger
        signal it has and the user confirms it with the press that uses the
        table. A failure is reported rather than raised, because the Review
        screen it serves is usable without it.
        """
        try:
            found = game_executables(self.paths.user_home, app_id)
        except (OSError, ValueError, RuntimeError) as exc:
            log_activity(
                self.logger, "warning", "game_files.unreadable",
                app_id=app_id if isinstance(app_id, int) else None, reason=str(exc)[:256],
            )
            return {
                "schema": 3, "app_id": app_id if isinstance(app_id, int) else None,
                "install_dir": None, "executables": [], "truncated": False,
                "source": None, "declared_reason": None,
                "reason": "this game's files could not be read",
                "cause": "libraries_unreadable",
            }
        log_activity(
            self.logger, "info", "game_files.listed",
            app_id=app_id, found=len(found["executables"]),
            truncated=bool(found["truncated"]), reason=found["reason"],
            # Which answer this is. A review offering a folder full of
            # candidates rather than the one program Steam starts is a support
            # question, and the reason it fell back is the whole of the answer.
            source=found["source"], declared_reason=found["declared_reason"],
            cause=found["cause"],
        )
        return found

    async def resume_recovered_supervision(self) -> list[int]:
        """Re-supervise every owned Cheat Engine that outlived the previous load."""
        return await self.ce_launch.resume_all_recovered_supervision()

    def _packaged_bridge_sha256(self) -> str | None:
        try:
            return sha256(self.bridge_source.read_bytes()).hexdigest()
        except OSError:
            return None

    def _recovered_bridge_mismatch(
        self,
        recovered: object,
        *,
        expected_ce_sha256: str | None = None,
    ) -> str | None:
        """Whether a recovered Cheat Engine is running a bridge this build replaced.

        An attached launch survives a plugin update on purpose. The runtime
        identity that binds the bridge lives in the runtime directory name, so a
        new backend could recover the still-running Cheat Engine, validate its
        old status against its old descriptor, and report it connected - while
        the frontend issued current-version commands to the previous resident
        protocol. That is safe only for as long as every bridge change happens
        to stay compatible in both directions.
        """
        if not isinstance(recovered, dict):
            return None
        recorded = recovered.get("bridge_sha256")
        if not isinstance(recorded, str) or not recorded:
            # Written before the bridge identity was recorded. Say so rather
            # than claiming a match we cannot prove.
            return "this Cheat Engine was started by an earlier CE Decky and its resident bridge cannot be identified"
        packaged = self._packaged_bridge_sha256()
        if packaged is None:
            return "this CE Decky build cannot identify its packaged resident bridge"
        if recorded.lower() != packaged.lower():
            return "this Cheat Engine is still running the resident bridge from a previous CE Decky version"
        # The bridge is only one third of the runtime identity the project binds:
        # the executable SHA, the complete source-tree SHA and the bridge SHA
        # together name the private runtime directory this process is executing
        # from. Its manifest holds all three, so reading that one small file is
        # an exact check rather than a proxy - and a directory that has since
        # been superseded or collected is itself the answer.
        executable = recovered.get("executable")
        if not isinstance(executable, str) or not executable:
            return "the private runtime this Cheat Engine is executing from can no longer be identified"
        try:
            validate_runtime_identity_manifest(
                runtime_parent=self.paths.ce_root / "runtime",
                executable=Path(executable),
                expected_executable_sha256=expected_ce_sha256,
                expected_bridge_sha256=packaged,
            )
        except (OSError, ValueError) as exc:
            return f"the private runtime this Cheat Engine is executing from is incompatible: {str(exc)[:256]}"
        return None

    def get_ce_launch_capability(self, app_id: int | None = None) -> dict[str, object]:
        """Report whether Cheat Engine can be launched, and for which target."""
        if app_id is not None and (isinstance(app_id, bool) or not isinstance(app_id, int)):
            raise ValueError("AppID must be an integer or null")
        capability = self.ce_launch.capability(app_id)
        try:
            tools = discover_proton_tools(self.paths.user_home)
            reason: str | None = None
        except (OSError, RuntimeError, ValueError) as exc:
            tools, reason = [], f"installed Proton tools could not be enumerated: {exc}"
        capability["recovered_bridge_mismatch"] = self._recovered_bridge_mismatch(capability.get("recovered"))
        capability["proton_tools"] = [tool.public() for tool in tools]
        capability["reason"] = reason
        observed_tool = None
        observed_reason: str | None = None
        game = capability.get("game")
        if isinstance(game, dict) and game.get("running") and tools:
            observation = observe_game_container(int(game["app_id"]))
            observed_tool, observed_reason = match_observed_proton(observation, tools)
        capability["observed_proton_tool"] = observed_tool.public() if observed_tool else None
        capability["observed_proton_reason"] = observed_reason
        try:
            imported = self._validated_ce_import()
            capability["ce_executable_sha256"] = imported.sha256
            capability["ce_ready"] = True
        except ValueError as exc:
            capability["ce_executable_sha256"] = None
            capability["ce_ready"] = False
            capability["reason"] = reason or str(exc)[:512]
        return capability

    async def start_ce_self_test(self, proton_tool_id: str) -> dict[str, object]:
        """Launch the private Cheat Engine runtime with no game and no Steam state."""
        tool, executable, ce_sha256 = await drained_to_thread(self._launch_inputs, proton_tool_id)
        return await self.ce_launch.start_self_test(tool, executable, ce_sha256)

    def poll_ce_launch(self, operation_id: str) -> dict[str, object]:
        return self.ce_launch.status(operation_id)

    async def stop_ce_launch(self, operation_id: str) -> dict[str, object]:
        return await self.ce_launch.stop(operation_id)

    async def launch_ce_for_game(self, app_id: int, proton_tool_id: str | None = None) -> dict[str, object]:
        """Start Cheat Engine inside the running game's own compatibility prefix.

        No Steam launch option is written and the game is not restarted; the
        prepared exact-SHA session decides which table Cheat Engine loads.
        """
        if proton_tool_id is not None and not isinstance(proton_tool_id, str):
            raise ValueError("Proton tool identity must be a string or null")
        try:
            # Session/runtime preparation mutates plugin-owned files and creates
            # a launch reservation. Cancellation must not abandon that worker:
            # unload may return only after the mutation is drained, and the
            # reservation must be released even when preparation is cancelled.
            prepared, executable, bridge_sha256, target_process = await drained_to_thread(
                self._attached_launch_inputs, app_id
            )
            tools = await asyncio.to_thread(discover_proton_tools, self.paths.user_home)
            await asyncio.to_thread(self._revalidate_launch_reservation, prepared)
            return await self.ce_launch.start_attached(
                tools,
                executable,
                app_id=prepared.app_id,
                tool_id=proton_tool_id,
                session_id=prepared.session_id,
                descriptor_path=Path(prepared.descriptor_path),
                descriptor_sha256=prepared.descriptor_sha256,
                descriptor_md5=prepared.descriptor_md5,
                descriptor_windows_path=prepared.descriptor_windows_path,
                table_windows_path=wine_z_path(Path(prepared.descriptor_path).parent / "table.ct"),
                table_sha256=prepared.table_sha256,
                ce_sha256=prepared.ce_sha256,
                status_path=Path(prepared.status_path),
                bridge_sha256=bridge_sha256,
                # The exact process this game's profile confirmed. Cheat Engine
                # runs inside the game's own Wine prefix and keeps that prefix
                # alive, so this is the only identity whose disappearance can
                # prove the game itself is gone.
                target_process=target_process,
            )
        finally:
            await drained_to_thread(self._release_launch_reservation, app_id)

    async def stop_ce_for_game(self, app_id: int, table_sha256: str | None = None) -> dict[str, object]:
        """A Revoke stop carries exact table and captured owned session authority."""
        if table_sha256 is None:
            return await self.ce_launch.stop_for_app(app_id)
        with self._mutation_lock:
            digest = self._sha(table_sha256)
            profile = self.profile_store.get(app_id)
            current = self.session_store.load_current(app_id)
            if profile is None or profile.table_sha256 != digest or (current is not None and current.table_sha256 != digest):
                raise ValueError("selected table changed; refresh before revoking it")
            if current is None:
                self._assert_no_live_owned_launch(app_id)
                return {"stopped": False, "operation": None, "recovered": False}
            expected_session = current.session_id
            log_activity(self.logger, "info", "profile.revoke_stop_requested", app_id=app_id,
                         table_sha=digest[:12], session=expected_session[:12])
        return await self.ce_launch.stop_for_app(app_id, expected_session_id=expected_session)

    def _launch_inputs(self, proton_tool_id: str):
        if not isinstance(proton_tool_id, str) or not proton_tool_id.strip():
            raise ValueError("an exact installed Proton tool must be selected")
        tools = discover_proton_tools(self.paths.user_home)
        tool = next((item for item in tools if item.tool_id == proton_tool_id), None)
        if tool is None:
            raise ValueError("the selected Proton tool is no longer installed")
        runtime = self.prepare_private_ce_runtime()
        return tool, Path(str(runtime["executable"])), str(runtime["source_executable_sha256"])

    def _attached_launch_inputs(self, app_id: int):
        with self._mutation_lock:
            self._assert_no_live_owned_launch(app_id)
            # A readable-but-stale current session is exactly what an ordinary
            # identity change produces - switching table, changing the target
            # process, re-importing Cheat Engine - and refusing here made every
            # one of those permanently unlaunchable, because the fresh
            # `prepare_session()` two lines down was never reached. Replacement
            # is already guarded: `prepare_session()` proves through
            # `_assert_session_replacement_safe()` that no live owned Cheat
            # Engine and no fresh or uncertain bridge heartbeat depends on the
            # session being replaced. Unreadable state is the stricter repair
            # route and still propagates from `load_current()`.
            stale_reason: str | None = None
            current = self.session_store.load_current(app_id)
            if current is not None:
                stale_reason = self._session_stale_reason(current)
                if stale_reason is not None:
                    log_activity(
                        self.logger, "info", "session.stale_replaced",
                        app_id=app_id, session=current.session_id[:12], reason=stale_reason[:128],
                    )
            # A control log is replayable during one CE lifetime.  Never reuse
            # it across a new process: preparing here atomically gives every
            # attached CE launch a new UUID, empty control log and status path.
            #
            # This is also the only session creation an ordinary launch performs.
            # The caller used to have to prepare one first purely to satisfy a
            # precondition here, and each prepare writes a full exact-SHA table
            # snapshot, so every launch left one snapshot behind that Cheat
            # Engine never opened. A profile that is not launch-ready fails in
            # `prepare_session` with the exact blocker instead of a generic
            # "prepare a session first".
            self.prepare_session(app_id)
            prepared = self.session_store.load_current(app_id)
            if prepared is None:
                raise ValueError("fresh launch session could not be persisted")
            runtime = self.prepare_private_ce_runtime()
            if str(runtime["source_executable_sha256"]) != prepared.ce_sha256:
                raise ValueError(
                    "the prepared session was created for a different Cheat Engine executable identity"
                )
            self._launch_reservations[app_id] = (prepared.session_id, prepared.table_sha256, prepared.ce_sha256)
            # Read under the same lock that prepared the session, so the process
            # supervision watches is the one this exact session was built for.
            profile = self.profile_store.get(app_id)
            target_process = profile.target_process if profile is not None and profile.target_process else ""
            return prepared, Path(str(runtime["executable"])), str(runtime["bridge_sha256"]), target_process

    def _revalidate_launch_reservation(self, prepared) -> None:
        with self._mutation_lock:
            expected = self._launch_reservations.get(prepared.app_id)
            if expected != (prepared.session_id, prepared.table_sha256, prepared.ce_sha256):
                raise ValueError("Cheat Engine launch reservation was lost; refresh and retry")
            current = self.session_store.load_current(prepared.app_id)
            if current is None or current.session_id != prepared.session_id or self._session_stale_reason(current) is not None:
                raise ValueError("prepared session changed while the launch was being authorized")
            self._assert_no_live_owned_launch(prepared.app_id, allow_reservation=True)

    def _release_launch_reservation(self, app_id: int) -> None:
        with self._mutation_lock:
            self._launch_reservations.pop(app_id, None)

    def _assert_no_managed_ce_transition(self) -> None:
        if self._managed_ce_reservation is not None:
            raise ValueError("managed Cheat Engine setup is in progress; resume or cancel it before changing runtime identity")

    def _reconcile_managed_ce_reservation(self, operation: dict[str, object]) -> None:
        operation_id = operation.get("operation_id")
        state = operation.get("state")
        if not isinstance(operation_id, str) or not isinstance(state, str):
            return
        if self._managed_ce_reservation in {"starting", operation_id} and state in {"failed", "cancelled"}:
            self._managed_ce_reservation = None

    def _assert_no_live_owned_launch(self, app_id: int, *, allow_reservation: bool = False) -> None:
        if app_id in self._launch_reservations and not allow_reservation:
            raise ValueError("Cheat Engine launch authorization is in progress; wait for it to finish")
        if self.ce_launch.current_for_app(app_id) is not None:
            raise ValueError("cannot change this runtime identity while CE Decky owns a live Cheat Engine process")
        recovered = self.ce_launch.recover_owned_launch(app_id)
        if recovered is not None:
            raise ValueError("cannot change this runtime identity while a recovered Cheat Engine process is live")

    def _owned_launch_proven_gone(self, prepared) -> bool:
        """True when CE Decky started this exact session and its process is gone.

        The stale-heartbeat rule exists because a live resident bridge must never
        lose its session underneath it. When CE Decky owns the Cheat Engine
        process group itself and has observed its exit, a heartbeat written
        before that exit cannot belong to a live bridge. A status file written
        *after* the confirmed exit still fails closed.
        """
        exited_at = self.ce_launch.confirmed_exit_epoch(prepared.app_id, prepared.session_id)
        if exited_at is None:
            return False
        # Only the write time matters here, and a corrupt heartbeat must not be
        # able to withdraw a confirmed-exit proof, so this never parses content.
        try:
            status_mtime_ns = self.session_store.status_mtime_ns(prepared)
        except ValueError:
            return False
        if status_mtime_ns is None:
            return True
        return (status_mtime_ns / 1_000_000_000) <= exited_at

    def _assert_session_replacement_safe(self, app_id: int) -> None:
        self._assert_no_live_owned_launch(app_id)
        current = self.session_store.load_current(app_id)
        if current is None:
            return
        runtime = self.get_runtime_status(app_id)
        if self._bridge_blocks_session_change(runtime, current):
            raise ValueError(
                "cannot replace the current prepared session while the resident bridge has a fresh heartbeat or a clock-skewed heartbeat"
            )

    def _bridge_blocks_session_change(self, runtime: dict[str, object], prepared) -> bool:
        # An unreadable heartbeat is an unknown heartbeat, not an absent one: a
        # live bridge that wrote a truncated status must not have its session
        # replaced underneath it, so it joins clock skew as uncertainty.
        status_unreadable = bool(runtime.get("status_unreadable"))
        status_observed = runtime.get("status") is not None or status_unreadable
        heartbeat_uncertain_or_live = (
            bool(runtime.get("status_fresh")) or bool(runtime.get("status_clock_skew")) or status_unreadable
        )
        if not (status_observed and heartbeat_uncertain_or_live):
            return False
        return not self._owned_launch_proven_gone(prepared)

    def _runtime_protocol_state_reason(self, prepared) -> str | None:
        """Report unreadable mutable control/status protocol state, if any."""
        try:
            self.session_store.read_status_observation(prepared)
            self.session_store.next_generation(prepared)
        except ValueError as exc:
            return _runtime_protocol_reason(exc)
        return None

    def _session_stale_reason(self, prepared) -> str | None:
        try:
            profile = self.profile_store.get(prepared.app_id)
        except ValueError as exc:
            return f"profile state is invalid: {str(exc)[:256]}"
        if profile is None:
            return "game profile no longer exists"
        if prepared.is_shortcut is None:
            return "prepared session predates exact Steam/shortcut identity; retire it and prepare a new session"
        if profile.is_shortcut != prepared.is_shortcut:
            return "profile Steam/shortcut identity changed"
        if profile.table_sha256 != prepared.table_sha256:
            return "profile selected table SHA changed"
        if profile.execution_consent_sha256 != prepared.table_sha256:
            return "exact-SHA execution consent was revoked or changed"
        if not profile.target_process:
            return "profile target process is no longer configured"
        try:
            descriptor = self.session_store.validated_descriptor(prepared)
        except ValueError as exc:
            return f"session descriptor is invalid: {str(exc)[:256]}"
        if (profile.target_process or "").casefold() != (descriptor.target_process or "").casefold():
            return "profile target process changed"
        if descriptor.table_has_lua is None:
            # The bridge decides on this whether it may use the table-load route
            # that lets Cheat Engine ask about the table's own Lua script, and a
            # session prepared before the field existed states nothing. Retiring
            # it costs one preparation and removes the only case where that
            # decision would have to be guessed at.
            return "prepared session predates the table's executable-script identity; retire it and prepare a new session"
        try:
            config = self.config_store.load()
        except ValueError as exc:
            return f"config state is invalid: {str(exc)[:256]}"
        if config.imported_ce_sha256 != prepared.ce_sha256:
            return "configured Cheat Engine identity changed"
        return None

    def _validated_ce_import(self):
        config = self.config_store.load()
        if not config.imported_ce_executable or not config.imported_ce_sha256 or not config.imported_ce_root:
            raise ValueError("Cheat Engine is not fully configured")
        path = Path(config.imported_ce_executable)
        if not path.is_file():
            raise ValueError("configured executable is missing")
        inspected = inspect_ce_selection(str(path))
        resolved_executable = Path(inspected.executable)
        try:
            resolved_executable.relative_to(self.paths.user_home)
        except ValueError as exc:
            raise ValueError("configured Cheat Engine path now resolves outside DECKY_USER_HOME; re-import is required") from exc
        try:
            resolved_executable.relative_to(self.paths.managed_root)
        except ValueError:
            pass
        else:
            # A plugin-owned tree is re-validated against the manifest written
            # when it was promoted, so an edited or half-replaced installation
            # can never present itself as the reviewed one.
            imported_root = self.paths.ce_root / "imported"
            try:
                resolved_executable.relative_to(imported_root)
            except ValueError:
                inspected = validate_managed_installation(Path(inspected.root), self.paths.ce_root)
            else:
                inspected = validate_imported_installation(Path(inspected.root))
        if inspected.sha256 != config.imported_ce_sha256:
            raise ValueError("configured executable bytes changed since import; re-import is required")
        if Path(inspected.root) != Path(config.imported_ce_root):
            raise ValueError("configured Cheat Engine root changed since import; re-import is required")
        return inspected

    @staticmethod
    def _runtime_command(raw: dict[str, object]) -> RuntimeCommand:
        if not isinstance(raw, dict) or not set(raw).issubset(_ALLOWED_RUNTIME_COMMAND_KEYS):
            raise ValueError("runtime command contains unknown fields")
        if "generation" not in raw or "kind" not in raw:
            raise ValueError("runtime command requires generation and kind")
        generation = raw["generation"]
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1 or generation > 0x7FFFFFFF:
            raise ValueError("runtime command generation must be an integer between 1 and 2147483647; 0 is reserved for startup")
        kind = raw["kind"]
        if not isinstance(kind, str) or kind not in {"query", "set_active", "set_value", "retry_attach", "list_processes"}:
            raise ValueError("unsupported runtime command")
        rid = raw.get("record_id")
        if rid is not None and (isinstance(rid, bool) or not isinstance(rid, int) or rid < 0 or rid > 0x7FFFFFFF):
            raise ValueError("runtime MemoryRecord ID is invalid")
        value = raw.get("value")
        if value is not None and (not isinstance(value, str) or utf8_len(value, "runtime command value") > 4096):
            raise ValueError("runtime command value must be a bounded string or null")
        if kind in {"query", "set_active", "set_value"} and rid is None:
            raise ValueError("record runtime command requires MemoryRecord ID")
        if kind == "set_active" and value not in {"0", "1"}:
            raise ValueError("set_active requires value '0' or '1'")
        if kind == "set_value" and value is None:
            raise ValueError("set_value requires an explicit string value; use an empty string to clear")
        if kind in {"query", "list_processes"} and value is not None:
            raise ValueError(f"{kind} does not accept a value")
        if kind in {"retry_attach", "list_processes"} and rid is not None:
            raise ValueError(f"{kind} does not accept a MemoryRecord ID")
        if kind == "retry_attach" and value is not None:
            normalized = unicodedata.normalize("NFC", value)
            if (
                normalized != value
                or not _PROCESS_RE.fullmatch(value)
                or any(unicodedata.category(ch) in {"Cc", "Cf"} for ch in value)
                or any(unicodedata.bidirectional(ch) in {"RLE", "LRE", "RLO", "LRO", "PDF", "RLI", "LRI", "FSI", "PDI"} for ch in value)
            ):
                raise ValueError("retry_attach target must be an unambiguous basename ending in .exe")
        target_pid = raw.get("target_pid")
        if target_pid is not None and (isinstance(target_pid, bool) or not isinstance(target_pid, int) or target_pid < 1 or target_pid > 0xFFFFFFFF):
            raise ValueError("runtime target PID is invalid")
        if kind == "retry_attach" and target_pid is not None and value is None:
            raise ValueError("exact-PID retry attach requires the expected .exe basename")
        if kind != "retry_attach" and target_pid is not None:
            raise ValueError(f"{kind} does not accept a target PID")
        return RuntimeCommand(generation=generation, kind=kind, record_id=rid, value=value, target_pid=target_pid)

    @classmethod
    def _actionable_control_by_id(cls, controls: tuple[TableControl, ...], record_id: int) -> TableControl:
        control = cls._control_by_id(controls, record_id)
        if control.kind == "group" or control.group_header:
            raise ValueError("group-header MemoryRecords are presentation-only and cannot be mutated")
        return control

    @staticmethod
    def _control_by_id(controls: tuple[TableControl, ...], record_id: int) -> TableControl:
        if isinstance(record_id, bool) or not isinstance(record_id, int) or record_id < 0 or record_id > 0x7FFFFFFF:
            raise ValueError("MemoryRecord ID is invalid")
        matches = [control for control in controls if control.id == record_id]
        if len(matches) != 1:
            raise ValueError("MemoryRecord ID is missing or ambiguous in the exact table SHA")
        return matches[0]

    @staticmethod
    def _sha(value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("SHA-256 identity must be a string")
        digest = value.strip().lower()
        if not _SHA_RE.fullmatch(digest):
            raise ValueError("SHA-256 identity must be 64 hexadecimal characters")
        return digest

    @staticmethod
    def _operation_id(value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
            raise ValueError("managed CE install operation identity is invalid")
        return value

    def _sevenzip_opens_rar(self) -> bool:
        """Whether the 7-Zip this device resolves has a RAR handler.

        The archive adapter caches against the observed executable identity, so
        each search may ask without running a subprocess and an in-place package
        update is still observed. `7z` carries the handler and the reduced
        `7za`/`7zr` builds `_find_7zip` falls back to do not.
        """
        sevenzip = self._find_7zip()
        answer = sevenzip_opens_rar(sevenzip)
        observed = (sevenzip, answer)
        if self._rar_probe != observed:
            self._rar_probe = observed
            log_activity(self.logger, "info", "archive.rar_support", sevenzip=sevenzip, supported=answer)
        return answer

    @staticmethod
    def _find_7zip() -> str | None:
        for name in ("7z", "7za", "7zr"):
            hit = shutil.which(name)
            if hit:
                return str(Path(hit).resolve())
        return None

    def _bridge_asset_check(self) -> dict[str, object]:
        try:
            if self.bridge_source.is_symlink() or not self.bridge_source.is_file():
                raise ValueError("bridge asset is missing or not a regular file")
            size = self.bridge_source.stat().st_size
            if size <= 0 or size > 1024 * 1024:
                raise ValueError("bridge asset size is invalid")
            text = self.bridge_source.read_text(encoding="utf-8")
            for forbidden in ("dofile(", "loadfile(", "loadstring(", "load("):
                if forbidden in text:
                    raise ValueError(f"bridge contains forbidden mutable-code loader: {forbidden}")
            return {"name": "bridge_asset", "ok": True, "detail": f"{size} byte data-only Lua bridge", "blocking": True}
        except (OSError, UnicodeError, ValueError) as exc:
            return {"name": "bridge_asset", "ok": False, "detail": str(exc), "blocking": True}

    def _config_state_check(self) -> dict[str, object]:
        try:
            self.config_store.load()
            return {"name": "config_state", "ok": True, "detail": "config schema valid", "blocking": True}
        except ValueError as exc:
            return {"name": "config_state", "ok": False, "detail": str(exc), "blocking": True}

    def _table_state_check(self) -> dict[str, object]:
        try:
            tables, errors = self.table_store.list_tables_with_errors()
            if errors:
                return {"name": "table_catalog_state", "ok": False, "detail": f"{len(tables)} valid table metadata record(s); {len(errors)} corrupt record(s)", "blocking": True}
            return {"name": "table_catalog_state", "ok": True, "detail": f"{len(tables)} table metadata record(s) visible", "blocking": True}
        except ValueError as exc:
            return {"name": "table_catalog_state", "ok": False, "detail": str(exc), "blocking": True}

    def _profiles_state_check(self) -> dict[str, object]:
        try:
            count = len(self.profile_store.list_profiles())
            return {"name": "profiles_state", "ok": True, "detail": f"{count} profile(s), schema valid", "blocking": True}
        except ValueError as exc:
            return {"name": "profiles_state", "ok": False, "detail": str(exc), "blocking": True}

    def _writable_check(self, name: str, directory: Path) -> dict[str, object]:
        temp_path: Path | None = None
        try:
            fd, temp = tempfile.mkstemp(prefix=".selftest.", dir=directory)
            temp_path = Path(temp)
            with os.fdopen(fd, "wb") as handle:
                handle.write(b"ok")
                handle.flush()
                os.fsync(handle.fileno())
            temp_path.unlink(missing_ok=True)
            return {"name": name, "ok": True, "detail": str(directory), "blocking": True}
        except OSError as exc:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            return {"name": name, "ok": False, "detail": str(exc), "blocking": True}

    def _same_filesystem_check(self) -> dict[str, object]:
        try:
            same = self.paths.tables_root.stat().st_dev == self.paths.temp_root.stat().st_dev
            return {
                "name": "atomic_staging_filesystem",
                "ok": same,
                "detail": "tables and tmp share filesystem" if same else "tables and tmp are on different filesystems",
                "blocking": True,
            }
        except OSError as exc:
            return {"name": "atomic_staging_filesystem", "ok": False, "detail": str(exc), "blocking": True}


MAX_INVENTORY_ENTRIES = 100_000
# Under this, the plugin's own root is where ordinary work starts failing: a
# staged table import, a promoted Cheat Engine tree, a cache write. It is a
# warning rather than a refusal, because what the user can do about it is
# outside this plugin.
MIN_FREE_MANAGED_BYTES = 512 * 1024 * 1024


# The three scopes the panel offers, each a superset of the one before it. They
# are named here rather than assembled from a caller-supplied list so a request
# can never reach a path CE Decky does not own.
MANAGED_DATA_SCOPES: dict[str, tuple[str, ...]] = {
    "cache": ("cache", "tmp", "logs"),
    # `settings` is deliberately absent here. Which Cheat Engine is registered
    # lives in `config.json` under the Decky settings directory, so deleting it
    # while keeping `ce` left a multi-gigabyte installation on disk that the
    # panel reported as not installed and no scope but `all` could reach.
    "setup": ("cache", "tmp", "logs", "state"),
    "all": ("cache", "tmp", "logs", "settings", "state", "tables", "ce"),
}


def _clear_managed_directory(key: str, label: str, path: Path) -> dict[str, object]:
    """Remove everything inside one owned directory, keeping the directory.

    A symlink anywhere in the path is refused rather than followed: this deletes
    recursively, and following one would delete outside the plugin's own tree.
    Individual failures are reported rather than raised so one unreadable entry
    cannot leave the rest of a confirmed deletion undone.
    """
    removed_files = 0
    removed_bytes = 0
    error: str | None = None
    try:
        if path.is_symlink():
            raise ValueError("path is a symlink")
        if not path.is_dir():
            return {"key": key, "label": label, "removed_files": 0, "removed_bytes": 0, "error": None}
        for child in sorted(path.iterdir()):
            if child.is_symlink():
                child.unlink()
                removed_files += 1
                continue
            if child.is_dir():
                # `Path.rglob` follows symlinked directories on Python 3.11, so
                # counting with it walked outside the plugin's own tree and could
                # loop forever on a link cycle - inside a destructive call. This
                # is the same bounded, symlink-refusing scan the size report uses.
                files, total, _truncated, _error = _bounded_directory_size(child)
                removed_files += files
                removed_bytes += total
                # `shutil.rmtree` unlinks symlinked directories rather than
                # descending into them, so deletion itself never leaves the tree.
                shutil.rmtree(child)
                continue
            try:
                removed_bytes += child.stat().st_size
            except OSError:
                pass
            child.unlink()
            removed_files += 1
        try:
            fsync_directory(path)
        except OSError as exc:
            raise DurabilityUnknownError(f"directory contents were removed; durability is unknown: {exc}") from exc
    except (OSError, ValueError) as exc:
        error = str(exc)[:512]
    return {
        "key": key, "label": label,
        "removed_files": removed_files, "removed_bytes": removed_bytes, "error": error,
    }


def _bounded_directory_size(root: Path) -> tuple[int, int, bool, str | None]:
    """Files and bytes under `root`, stopping at a bound rather than walking forever.

    A root that is itself a symlink is refused rather than followed. Entries
    inside were already skipped, but `is_dir()` follows the link, so a managed
    root replaced after initialization was measured through it - reporting
    somebody else's bytes as the plugin's own, and contradicting the promise
    that symlinks are not followed even to measure.
    """
    if root.is_symlink():
        return 0, 0, False, "path is a symlink"
    if not root.is_dir():
        return 0, 0, False, None
    files = 0
    entries_seen = 0
    total = 0
    truncated = False
    try:
        stack = [root]
        while stack:
            current = stack.pop()
            with os.scandir(current) as entries:
                for entry in entries:
                    if entries_seen >= MAX_INVENTORY_ENTRIES:
                        return files, total, True, None
                    entries_seen += 1
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir():
                            stack.append(Path(entry.path))
                            continue
                        files += 1
                        total += entry.stat().st_size
                    except OSError:
                        # One unreadable entry is not a reason to report nothing
                        # about a directory the user is deciding whether to keep.
                        truncated = True
    except OSError as exc:
        return files, total, truncated, str(exc)[:256]
    return files, total, truncated, None
