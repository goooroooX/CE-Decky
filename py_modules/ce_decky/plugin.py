from __future__ import annotations

import asyncio

import decky

from . import poll_counters
from . import __version__
from .activity_log import configure_dependency_logging, log_activity, log_failure
from .operations import OperationRegistry
from .paths import PluginPaths
from .guarded_service import GuardedPluginService


def _counted(path: str, call):
    """Count one repeating RPC where it actually runs.

    The count belongs at this boundary rather than inside the service, because
    what is being attributed is the panel's own interval: the support bundle and
    the guarded writer call the same service methods for their own reasons, and
    a counter that included them would answer a different question.

    The wrapper goes around the callable `run_blocking` hands to its worker
    thread, not around the await. Processor time is per thread, so timing the
    await would measure the event loop rather than the work.
    """

    def counted(*args):
        with poll_counters.timed(path):
            return call(*args)

    return counted


# When the unload timer probe below fires, well inside Decky's five second stop
# budget and well after any healthy drain has finished.
UNLOAD_TIMER_PROBE_S = 0.5


def _loop_identity() -> str:
    """A short name for the running loop, for comparing one call against another.

    Decky calls `_main`, every RPC and `_unload` itself; whether those share one
    event loop decides whether work started on one can be drained by another,
    and nothing in a log line says so unless it is put there. Identity only: it
    is never used to decide anything.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return "none"
    return f"{id(loop) % 1000000:06d}{'-running' if loop.is_running() else '-stopped'}"


class Plugin:
    def __init__(self) -> None:
        self.operations = OperationRegistry(decky.logger)
        self.service: GuardedPluginService | None = None

    async def _main(self) -> None:
        configure_dependency_logging()
        # Load has the same shape as unload: everything after this line is work
        # that can fail or block, and the record that says what it was doing
        # comes after it. `backend.initialized` at the end answers a load that
        # worked; this answers one that never got there.
        log_activity(decky.logger, "info", "backend.initialize_started", version=__version__)
        try:
            paths = PluginPaths.from_environment()
            self.service = GuardedPluginService(paths, decky.logger)
            self.service.initialize()
        except Exception as exc:
            log_failure(decky.logger, "backend.initialize_failed", exc, expected=False)
            raise
        # An attached Cheat Engine outlives a plugin update on purpose. Recovery
        # proves ownership but starts no monitor, so without this the process
        # that would normally stop itself when the game exits keeps running.
        try:
            resumed = await self.service.resume_recovered_supervision()
            if resumed:
                log_activity(decky.logger, "info", "backend.recovered_supervision_resumed", apps=len(resumed))
        except Exception as exc:  # noqa: BLE001 - never block load on a recovery probe
            log_failure(decky.logger, "backend.recovered_supervision_failed", exc, expected=False)
        # The provider listing refreshes itself from here on. It needs the
        # running loop this coroutine is on, which is why it is not part of the
        # synchronous initialization above.
        try:
            self.service.start_background_work()
        except Exception as exc:  # noqa: BLE001 - never block load on background work
            log_failure(decky.logger, "backend.background_work_failed", exc, expected=False)
        # The loop identity travels with both ends of the lifecycle, because the
        # one question a drain-on-unload design turns on is whether the loop
        # that owns the background work is the loop unload runs on.
        log_activity(decky.logger, "info", "backend.initialized", loop=_loop_identity())

    async def _unload(self) -> None:
        log_activity(decky.logger, "info", "backend.unload_started", loop=_loop_identity())
        # Stop accepting RPC work and drain tracked worker-thread mutations before
        # closing service-owned acquisition/network resources. Cancelling an
        # asyncio.to_thread awaiter does not stop its underlying mutation, so the
        # operation registry drains and then re-raises the cancellation. Closing
        # the service is what retires owned Cheat Engine processes, staged files
        # and network sessions, so neither a cancelled nor a failed drain may
        # skip it; the first outcome is preserved once both have run.
        # Before the first `await` of this coroutine, because on this host that
        # is the last moment anything is guaranteed to happen: Decky's stop
        # starves this process's event loop and kills it five seconds later, so
        # the orderly close below runs only when something else has not already
        # taken the loop away. What must happen does not wait for it.
        if self.service is not None:
            try:
                self.service.begin_close()
            except Exception as exc:  # noqa: BLE001 - never block unload on the prologue
                log_failure(decky.logger, "backend.begin_close_failed", exc, expected=False)
        # The first suspension point of the whole unload, and it is deliberate.
        # Everything below this line drains, and every drain is an await: if
        # this process's loop no longer resumes what it suspends, nothing below
        # can finish and no record below can be written, which is exactly the
        # shape of a plugin that is SIGKILLed five seconds into its own unload
        # having said nothing about why. One tick costs nothing when the loop is
        # healthy, and when it is not, the absence of the record below is the
        # only evidence there is.
        await asyncio.sleep(0)
        log_activity(decky.logger, "info", "backend.unload_loop_resumed")
        # Two probes the loop itself answers, independently of this coroutine.
        # A ready callback and a timer are the two things every drain below
        # needs, and they fail separately: a loop that is about to stop still
        # runs the callbacks already queued for its last pass, and never fires a
        # timer again. Their absence from the record is the evidence, which is
        # why they are scheduled here rather than awaited anywhere.
        loop = asyncio.get_running_loop()
        loop.call_soon(log_activity, decky.logger, "info", "backend.unload_ready_callback_ran")
        loop.call_later(
            UNLOAD_TIMER_PROBE_S, log_activity, decky.logger, "info", "backend.unload_timer_fired",
        )
        cancelled = False
        failure: Exception | None = None
        closes = [self.operations.close]
        if self.service is not None:
            closes.append(self.service.close)
        for close in closes:
            try:
                await close()
            except asyncio.CancelledError:
                cancelled = True
            except Exception as exc:
                log_failure(decky.logger, "backend.unload_failed", exc, expected=False)
                if failure is None:
                    failure = exc
        if cancelled:
            raise asyncio.CancelledError
        if failure is not None:
            raise failure
        log_activity(decky.logger, "info", "backend.unload_completed")

    async def _uninstall(self) -> None:
        # Deliberately non-destructive. Decky uses uninstall semantics during
        # update and reinstall, so this runs on an ordinary plugin update, and
        # the managed root holds the user's own tables, authorizations, profiles
        # and downloaded Cheat Engine. Deleting it here would destroy that on
        # every update; removal is an explicit user workflow instead.
        log_activity(decky.logger, "info", "backend.uninstall_preserved_state")

    def _svc(self) -> GuardedPluginService:
        if self.service is None:
            exc = RuntimeError("backend not initialized")
            log_failure(decky.logger, "backend.rpc_rejected", exc, expected=True, reason="not_initialized")
            raise exc
        if self.operations.closing:
            exc = RuntimeError("plugin is unloading")
            log_failure(decky.logger, "backend.rpc_rejected", exc, expected=True, reason="unloading")
            raise exc
        return self.service

    async def get_status(self, current_app_id: int | None = None):
        return await self.operations.run_blocking(self._svc().get_status, current_app_id)

    async def run_self_test(self):
        return await self.operations.run_blocking(self._svc().self_test)

    async def import_ce(self, selection: str):
        return await self.operations.run_blocking(self._svc().import_ce, selection)

    async def import_ce_archive(self, selection: str):
        return await self.operations.run_blocking(self._svc().import_ce_archive, selection)

    async def clear_ce_import(self):
        await self.operations.run_blocking(self._svc().clear_ce_import)
        return {"ok": True}

    async def inspect_table_source(self, selection: str):
        return await self.operations.run_blocking(self._svc().inspect_table_source, selection)

    async def import_table(self, selection: str, member_path: str | None = None, password: str | None = None, app_id: int | None = None):
        return await self.operations.run_blocking(self._svc().import_table, selection, member_path, password, app_id)

    async def inspect_table_sha(self, digest: str, app_id: int | None = None):
        return await self.operations.run_blocking(self._svc().inspect_table_sha, digest, app_id)

    async def list_blocked_tables(self):
        return await self.operations.run_blocking(self._svc().list_blocked_tables)

    async def block_table(self, sha256: str, reason: str, app_id: int | None = None):
        return await self.operations.run_blocking(self._svc().block_table, sha256, reason, app_id)

    async def unblock_table(self, sha256: str):
        return await self.operations.run_blocking(self._svc().unblock_table, sha256)

    async def clear_blocked_tables(self):
        return await self.operations.run_blocking(self._svc().clear_blocked_tables)

    async def delete_table(self, sha256: str):
        return await self.operations.run_blocking(self._svc().delete_table, sha256)

    async def list_table_code(self, digest: str):
        return await self.operations.run_blocking(self._svc().list_table_code, digest)

    async def read_table_code(self, digest: str, section_id: str):
        return await self.operations.run_blocking(self._svc().read_table_code, digest, section_id)

    async def list_profiles(self):
        return await self.operations.run_blocking(self._svc().list_profiles)

    async def get_provider_capabilities(self):
        return await self.operations.run_blocking(self._svc().get_provider_capabilities)

    async def search_tables(self, game_identity: dict[str, object], progress_token: str | None = None):
        return await self.operations.create(
            self._svc().search_tables(game_identity, progress_token), label="search_tables",
        )

    async def poll_table_search(self, progress_token: str):
        return await self.operations.run_blocking(self._svc().poll_table_search, progress_token)

    async def start_table_acquisition(self, provider: str, artifact_id: str, search_id: str | None = None, app_id: int | None = None):
        return await self.operations.create(self._svc().start_table_acquisition(provider, artifact_id, search_id, app_id), label="start_table_acquisition")

    async def poll_table_acquisition(self, acquisition_id: str):
        return await self.operations.run_blocking(self._svc().poll_table_acquisition, acquisition_id)

    async def complete_table_acquisition(self, acquisition_id: str, picked_path: str | None = None, member_path: str | None = None, password: str | None = None):
        await self.operations.create(self._svc().wait_table_acquisition(acquisition_id), label="wait_table_acquisition")
        return await self.operations.run_blocking(
            self._svc().complete_table_acquisition, acquisition_id, picked_path, member_path, password
        )

    async def cancel_table_acquisition(self, acquisition_id: str):
        return await self.operations.create(self._svc().cancel_table_acquisition(acquisition_id), label="cancel_table_acquisition")

    async def get_managed_ce_capability(self):
        return await self.operations.run_blocking(self._svc().get_managed_ce_capability)

    async def start_managed_ce_install(self, force: bool = False):
        return await self.operations.create(self._svc().start_managed_ce_install(force), label="start_managed_ce_install")

    async def poll_managed_ce_install(self, operation_id: str):
        return await self.operations.run_blocking(self._svc().poll_managed_ce_install, operation_id)

    async def complete_managed_ce_install(self, operation_id: str):
        await self.operations.create(self._svc().wait_managed_ce_install(operation_id), label="wait_managed_ce_install")
        return await self.operations.run_blocking(self._svc().complete_managed_ce_install, operation_id)

    async def cancel_managed_ce_install(self, operation_id: str):
        return await self.operations.create(self._svc().cancel_managed_ce_install(operation_id), label="cancel_managed_ce_install")

    async def plan_provider_search(self, display_name: str, shortcut_executable: str | None = None):
        return await self.operations.run_blocking(self._svc().plan_provider_search, display_name, shortcut_executable)

    async def evaluate_provider_candidates(self, display_name: str, shortcut_executable: str | None, candidates: list[dict[str, object]], desired_platform: str | None = None):
        return await self.operations.run_blocking(
            self._svc().evaluate_provider_candidates, display_name, shortcut_executable, candidates, desired_platform
        )

    async def get_provider_diagnostics(self):
        return await self.operations.run_blocking(self._svc().get_provider_diagnostics)

    async def clear_provider_diagnostics(self, provider_id: str):
        return await self.operations.run_blocking(self._svc().clear_provider_diagnostics, provider_id)

    async def reset_provider_diagnostics(self):
        # Drains interactive searches before replacing the record they write to,
        # so it owns its own worker boundary rather than going through
        # run_blocking.
        return await self.operations.create(
            self._svc().reset_provider_diagnostics(), label="reset_provider_diagnostics",
        )

    async def get_provider_sources(self):
        return await self.operations.run_blocking(self._svc().get_provider_sources)

    async def set_provider_enabled(self, provider_id: str, enabled: bool):
        return await self.operations.run_blocking(self._svc().set_provider_enabled, provider_id, enabled)

    async def reset_provider_sources(self):
        return await self.operations.run_blocking(self._svc().reset_provider_sources)

    async def set_update_auto_check(self, enabled: bool):
        return await self.operations.run_blocking(self._svc().set_update_auto_check, enabled)

    async def set_mascot_visible(self, visible: bool):
        return await self.operations.run_blocking(self._svc().set_mascot_visible, visible)

    async def check_for_update(self):
        return await self.operations.create(self._svc().check_for_update(), label="check_for_update")

    async def start_plugin_update(self, expected_version: str):
        return await self.operations.create(
            self._svc().start_plugin_update(expected_version), label="start_plugin_update",
        )

    async def poll_plugin_update(self, operation_id: str):
        return await self.operations.run_blocking(self._svc().poll_plugin_update, operation_id)

    async def cancel_plugin_update(self, operation_id: str):
        return await self.operations.create(
            self._svc().cancel_plugin_update(operation_id), label="cancel_plugin_update",
        )

    async def get_session_inventory(self):
        return await self.operations.run_blocking(self._svc().get_session_inventory)

    async def get_removal_readiness(self):
        return await self.operations.run_blocking(self._svc().get_removal_readiness)

    async def delete_managed_data(self, scope: str):
        # Deletion quiesces the provider index before touching its files, so it
        # owns its own worker boundary rather than going through run_blocking.
        return await self._svc().delete_managed_data(scope)

    async def diagnostics_snapshot(self):
        return await self.operations.run_blocking(self._svc().diagnostics_snapshot)

    async def create_support_bundle(self, frontend_log=None, frontend_dropped: int = 0):
        return await self.operations.run_blocking(self._svc().create_support_bundle, frontend_log, frontend_dropped)

    async def record_panel_log(self, entries=None, dropped: int = 0, session: str = ""):
        return await self.operations.run_blocking(self._svc().record_panel_log, entries, dropped, session)

    async def save_profile(self, app_id: int, name: str, is_shortcut: bool, table_sha256: str | None = None, target_process: str | None = None):
        return await self.operations.run_blocking(self._svc().save_profile, app_id, name, is_shortcut, table_sha256, target_process)

    async def delete_profile(self, app_id: int):
        return await self.operations.run_blocking(self._svc().delete_profile, app_id)

    async def set_execution_consent(self, app_id: int, table_sha256: str, consent: bool):
        return await self.operations.run_blocking(self._svc().set_execution_consent, app_id, table_sha256, consent)

    async def revoke_table(self, app_id: int, table_sha256: str):
        return await self.operations.run_blocking(self._svc().revoke_table, app_id, table_sha256)

    async def set_startup_preference(self, app_id: int, table_sha256: str, record_id: int, active=None, value=None):
        return await self.operations.run_blocking(
            self._svc().set_startup_preference, app_id, table_sha256, record_id, active, value
        )

    async def clear_startup_preference(self, app_id: int, table_sha256: str, record_id: int | None = None):
        return await self.operations.run_blocking(self._svc().clear_startup_preference, app_id, table_sha256, record_id)

    async def associate_table(self, app_id: int, table_sha256: str):
        return await self.operations.run_blocking(self._svc().associate_table, app_id, table_sha256)

    async def set_autoload(self, app_id: int, table_sha256: str, enabled: bool):
        return await self.operations.run_blocking(self._svc().set_autoload, app_id, table_sha256, enabled)

    async def set_remembered_cheats(self, app_id: int, table_sha256: str, states: list[dict[str, object]]):
        return await self.operations.run_blocking(self._svc().set_remembered_cheats, app_id, table_sha256, states)

    async def set_configured_values(self, app_id: int, table_sha256: str, values: list[dict[str, object]]):
        return await self.operations.run_blocking(self._svc().set_configured_values, app_id, table_sha256, values)

    async def set_pinned_control(self, app_id: int, table_sha256: str, record_id: int, pinned: bool):
        return await self.operations.run_blocking(self._svc().set_pinned_control, app_id, table_sha256, record_id, pinned)

    async def clear_pinned_controls(self, app_id: int, table_sha256: str):
        return await self.operations.run_blocking(self._svc().clear_pinned_controls, app_id, table_sha256)

    async def repair_session_state(self, app_id: int):
        return await self.operations.run_blocking(self._svc().repair_session_state, app_id)

    async def validate_effective_startup_plan(self, app_id: int, table_sha256: str, remembered=None, configured_values=None):
        return await self.operations.run_blocking(
            self._svc().validate_effective_startup_plan, app_id, table_sha256, remembered, configured_values,
        )

    async def repair_profile_state(self):
        return await self.operations.run_blocking(self._svc().repair_profile_state)

    async def repair_owned_launch_state(self, app_id: int):
        return await self.operations.run_blocking(self._svc().repair_owned_launch_state, app_id)

    async def prepare_session(self, app_id: int):
        return await self.operations.run_blocking(self._svc().prepare_session, app_id)

    async def get_runtime_status(self, app_id: int):
        return await self.operations.run_blocking(
            _counted(poll_counters.RPC_RUNTIME_STATUS, self._svc().get_runtime_status), app_id,
        )

    async def retire_session(self, app_id: int, expected_session_id: str):
        return await self.operations.run_blocking(self._svc().retire_session, app_id, expected_session_id)

    async def confirm_table_working(self, app_id: int, digest: str, session_id: str, record_id: int):
        return await self.operations.run_blocking(self._svc().confirm_table_working, app_id, digest, session_id, record_id)

    async def write_runtime_commands(self, app_id: int, commands: list[dict[str, object]]):
        return await self.operations.run_blocking(self._svc().write_runtime_commands, app_id, commands)

    async def prepare_private_ce_runtime(self):
        return await self.operations.run_blocking(self._svc().prepare_private_ce_runtime)

    async def list_running_app_ids(self):
        return await self.operations.run_blocking(self._svc().list_running_app_ids)

    async def list_game_executables(self, app_id: int):
        return await self.operations.run_blocking(self._svc().list_game_executables, app_id)

    async def local_library(self):
        return await self.operations.run_blocking(self._svc().local_library)

    async def get_ce_launch_capability(self, app_id: int | None = None):
        return await self.operations.run_blocking(
            _counted(poll_counters.RPC_CE_LAUNCH_CAPABILITY, self._svc().get_ce_launch_capability), app_id,
        )

    async def get_poll_counters(self):
        # Deliberately not routed through `run_blocking`: the snapshot is a
        # dictionary copy, and handing it to a worker thread would add a wakeup
        # to the very process whose wakeups this is here to explain. Nothing it
        # touches is counted, so reading the counters never becomes one of the
        # counts.
        return self._svc().get_poll_counters()

    async def start_ce_self_test(self, proton_tool_id: str):
        return await self.operations.create(self._svc().start_ce_self_test(proton_tool_id), label="start_ce_self_test")

    async def poll_ce_launch(self, operation_id: str):
        return await self.operations.run_blocking(self._svc().poll_ce_launch, operation_id)

    async def stop_ce_launch(self, operation_id: str):
        return await self.operations.create(self._svc().stop_ce_launch(operation_id), label="stop_ce_launch")

    async def launch_ce_for_game(self, app_id: int, proton_tool_id: str | None = None):
        return await self.operations.create(self._svc().launch_ce_for_game(app_id, proton_tool_id), label="launch_ce_for_game")

    async def stop_ce_for_game(self, app_id: int, table_sha256: str | None = None):
        operation = self._svc().stop_ce_for_game(app_id) if table_sha256 is None else self._svc().stop_ce_for_game(app_id, table_sha256)
        return await self.operations.create(operation, label="stop_ce_for_game")
