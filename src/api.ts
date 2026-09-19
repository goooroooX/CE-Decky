import { callable } from "@decky/api";
import type { SupportLogEntry } from "./supportLog";
import type {
  TableSearchProgress,
  BlockedTable,
  BlockedTableList,
  GameProfile,
  CELaunchCapability,
  CELaunchStatus,
  ManagedCECapability,
  ManagedCECompletion,
  ManagedCEInstallStatus,
  PanelPreferences,
  PluginStatus,
  PluginUpdateOperation,
  PluginUpdateState,
  PreparedSession,
  ProviderDefinition,
  ProviderCandidateInput,
  ProviderMatchDecision,
  ProviderSearchPlan,
  ProviderDiagnosticsSnapshot,
  ProviderSourcesSnapshot,
  ManagedDataDeletion,
  ManagedDataScope,
  RemovalReadiness,
  DiagnosticsSnapshot,
  SessionInventory,
  RuntimeEnvelope,
  SelfTestResult,
  TableCodeBody,
  TableCodeIndex,
  TableInspection,
  TableSourceInspection,
  TableStatus,
  StartupPreference,
  ConfiguredValue,
  CatalogSearchOutcome,
  AcquisitionStatus,
  GameExecutableListing,
  LocalLibrary,
  RunningAppIdObservation,
  SupportBundleResult,
} from "./types";

export const getStatus = callable<[currentAppId?: number | null], PluginStatus>("get_status");
export const runSelfTest = callable<[], SelfTestResult>("run_self_test");
export const importCE = callable<[selection: string], { executable: string; root: string; sha256: string; size: number }>("import_ce");
export const importCEArchive = callable<[selection: string], {
  executable: string; root: string; sha256: string; size: number;
  archive_sha256: string; archive_root: string; file_count: number; total_bytes: number;
}>("import_ce_archive");
export const clearCEImport = callable<[], { ok: boolean }>("clear_ce_import");
export const inspectTableSource = callable<[selection: string], TableSourceInspection>("inspect_table_source");
export const importTable = callable<[selection: string, memberPath?: string | null, password?: string | null, appId?: number | null], TableStatus>("import_table");
export const inspectTableSha = callable<[digest: string, appId?: number | null], TableInspection>("inspect_table_sha");
// A table's own executable content, read and never run. Two calls because one
// table on this device carries half a megabyte of scripts: the index says what
// is in it, and a section is fetched when the user opens it.
export const listTableCode = callable<[digest: string], TableCodeIndex>("list_table_code");
export const readTableCode = callable<[digest: string, sectionId: string], TableCodeBody>("read_table_code");
export const listProfiles = callable<[], GameProfile[]>("list_profiles");
export const listBlockedTables = callable<[], BlockedTableList>("list_blocked_tables");
export const blockTable = callable<[sha256: string, reason: string, appId: number | null], BlockedTable>("block_table");
export const unblockTable = callable<[sha256: string], boolean>("unblock_table");
/** Destroy one stored table on an explicit press. Refused while a game has it selected. */
export const deleteTable = callable<[sha256: string], { sha256: string; size: number }>("delete_table");
export const revokeTable = callable<[appId: number, tableSha256: string], GameProfile>("revoke_table");
export const clearBlockedTables = callable<[], number>("clear_blocked_tables");
export const getManagedCECapability = callable<[], ManagedCECapability>("get_managed_ce_capability");
export const startManagedCEInstall = callable<[force?: boolean], ManagedCEInstallStatus>("start_managed_ce_install");
export const pollManagedCEInstall = callable<[operationId: string], ManagedCEInstallStatus>("poll_managed_ce_install");
export const completeManagedCEInstall = callable<[operationId: string], ManagedCECompletion>("complete_managed_ce_install");
export const cancelManagedCEInstall = callable<[operationId: string], ManagedCEInstallStatus>("cancel_managed_ce_install");
// Updating the plugin itself. The status call already carries what the panel
// draws, so these are the four presses and nothing else: the switch, the forced
// check, the install, and the cancel that only reaches the half this device
// still owns.
export const setUpdateAutoCheck = callable<[enabled: boolean], PluginUpdateState>("set_update_auto_check");
export const setMascotVisible = callable<[visible: boolean], PanelPreferences>("set_mascot_visible");
export const checkForUpdate = callable<[], PluginUpdateState>("check_for_update");
export const startPluginUpdate = callable<[expectedVersion: string], PluginUpdateOperation>("start_plugin_update");
export const pollPluginUpdate = callable<[operationId: string], PluginUpdateOperation>("poll_plugin_update");
export const cancelPluginUpdate = callable<[operationId: string], PluginUpdateOperation>("cancel_plugin_update");
export const getProviderCapabilities = callable<[], ProviderDefinition[]>("get_provider_capabilities");
export const searchTables = callable<[
  gameIdentity: { display_name: string; shortcut_executable?: string | null },
  progressToken?: string | null,
], CatalogSearchOutcome>("search_tables");
export const pollTableSearch = callable<[progressToken: string], TableSearchProgress | null>("poll_table_search");
export const startTableAcquisition = callable<[provider: string, artifactId: string, searchId?: string | null, appId?: number | null], AcquisitionStatus>("start_table_acquisition");
export const pollTableAcquisition = callable<[acquisitionId: string], AcquisitionStatus>("poll_table_acquisition");
export const completeTableAcquisition = callable<[
  acquisitionId: string,
  pickedPath?: string | null,
  memberPath?: string | null,
  password?: string | null,
], AcquisitionStatus>("complete_table_acquisition");
export const cancelTableAcquisition = callable<[acquisitionId: string], AcquisitionStatus>("cancel_table_acquisition");
export const planProviderSearch = callable<[displayName: string, shortcutExecutable?: string | null], ProviderSearchPlan>("plan_provider_search");
export const evaluateProviderCandidates = callable<[displayName: string, shortcutExecutable: string | null, candidates: ProviderCandidateInput[], desiredPlatform?: string | null], ProviderMatchDecision>("evaluate_provider_candidates");
export const getProviderDiagnostics = callable<[], ProviderDiagnosticsSnapshot>("get_provider_diagnostics");
export const clearProviderDiagnostics = callable<[providerId: string], { cleared: boolean }>("clear_provider_diagnostics");
export const getProviderSources = callable<[], ProviderSourcesSnapshot>("get_provider_sources");
export const setProviderEnabled = callable<[providerId: string, enabled: boolean], ProviderSourcesSnapshot>("set_provider_enabled");
export const resetProviderSources = callable<[], ProviderSourcesSnapshot>("reset_provider_sources");
export const resetProviderDiagnostics = callable<[], ProviderSourcesSnapshot>("reset_provider_diagnostics");
export const getRemovalReadiness = callable<[], RemovalReadiness>("get_removal_readiness");
export const deleteManagedData = callable<[scope: ManagedDataScope], ManagedDataDeletion>("delete_managed_data");
export const getDiagnosticsSnapshot = callable<[], DiagnosticsSnapshot>("diagnostics_snapshot");
export const createSupportBundle = callable<[
  frontendLog: SupportLogEntry[],
  frontendDropped: number,
], SupportBundleResult>("create_support_bundle");
// Diagnostics only, and deliberately not awaited by anything a user is waiting
// on: the panel hands over what it has recorded so the record outlives the
// renderer it lives in. A rejected flush is dropped, never retried.
export const recordPanelLog = callable<[
  entries: SupportLogEntry[],
  dropped: number,
  session: string,
], { ok: boolean; accepted: number }>("record_panel_log");
export const getSessionInventory = callable<[], SessionInventory>("get_session_inventory");
export const saveProfile = callable<[
  appId: number,
  name: string,
  isShortcut: boolean,
  tableSha256?: string | null,
  targetProcess?: string | null,
], GameProfile>("save_profile");
export const deleteProfile = callable<[appId: number], { deleted: boolean }>("delete_profile");
export const setExecutionConsent = callable<[appId: number, tableSha256: string, consent: boolean], GameProfile>("set_execution_consent");
export const setStartupPreference = callable<[
  appId: number,
  tableSha256: string,
  recordId: number,
  active?: boolean | null,
  value?: string | null,
], GameProfile>("set_startup_preference");
export const clearStartupPreference = callable<[
  appId: number,
  tableSha256: string,
  recordId?: number | null,
], GameProfile>("clear_startup_preference");
export const associateTable = callable<[appId: number, tableSha256: string], GameProfile>("associate_table");
export const setAutoload = callable<[appId: number, tableSha256: string, enabled: boolean], GameProfile>("set_autoload");
export const setRememberedCheats = callable<[
  appId: number,
  tableSha256: string,
  states: StartupPreference[],
], GameProfile>("set_remembered_cheats");
export const setConfiguredValues = callable<[
  appId: number,
  tableSha256: string,
  values: ConfiguredValue[],
], GameProfile>("set_configured_values");
export const setPinnedControl = callable<[appId: number, tableSha256: string, recordId: number, pinned: boolean], GameProfile>("set_pinned_control");
export const clearPinnedControls = callable<[appId: number, tableSha256: string], GameProfile>("clear_pinned_controls");
export const prepareSession = callable<[appId: number], PreparedSession>("prepare_session");
export const getRuntimeStatus = callable<[appId: number], RuntimeEnvelope>("get_runtime_status");
export const repairSessionState = callable<[appId: number], { discarded: boolean; was_symlink: boolean; app_id: number }>("repair_session_state");
export const validateEffectiveStartupPlan = callable<[
  appId: number,
  tableSha256: string,
  remembered: Array<{ record_id: number; active: boolean | null; value: string | null }> | null,
  configuredValues: Array<{ record_id: number; value: string }> | null,
], { action_count: number; limit: number; fits: boolean }>("validate_effective_startup_plan");
export const repairProfileState = callable<[], { discarded: boolean; quarantined: string; reason: string }>("repair_profile_state");
export const repairOwnedLaunchState = callable<[appId: number], { discarded: boolean; app_id: number; quarantined: string }>("repair_owned_launch_state");
export const retireSession = callable<[
  appId: number,
  expectedSessionId: string,
], { retired: boolean; session_id: string | null; requires_target_validation: boolean }>("retire_session");
export const writeRuntimeCommands = callable<[
  appId: number,
  commands: Array<{
    generation: number;
    kind: string;
    record_id?: number | null;
    value?: string | null;
    target_pid?: number | null;
  }>,
], { ok: boolean; count: number; next_generation: number }>("write_runtime_commands");
export const preparePrivateCERuntime = callable<[], {
  root: string;
  executable: string;
  source_executable_sha256: string;
  source_tree_sha256: string;
  bridge_sha256: string;
  file_count: number;
  total_bytes: number;
}>("prepare_private_ce_runtime");
export const getCELaunchCapability = callable<[appId?: number | null], CELaunchCapability>("get_ce_launch_capability");
export const listRunningAppIds = callable<[], RunningAppIdObservation>("list_running_app_ids");
/**
 * The Windows executables in one installed game's own folder.
 *
 * The weakest evidence about a target process and the only kind available
 * before the game has ever been started, which is exactly when a table that
 * names no process leaves Review with nothing to offer.
 */
export const listGameExecutables = callable<[appId: number], GameExecutableListing>("list_game_executables");
export const readLocalLibrary = callable<[], LocalLibrary>("local_library");
export const startCESelfTest = callable<[protonToolId: string], CELaunchStatus>("start_ce_self_test");
export const pollCELaunch = callable<[operationId: string], CELaunchStatus>("poll_ce_launch");
export const stopCELaunch = callable<[operationId: string], CELaunchStatus>("stop_ce_launch");
export const launchCEForGame = callable<[appId: number, protonToolId?: string | null], CELaunchStatus>("launch_ce_for_game");
export const stopCEForGame = callable<[appId: number, tableSha256?: string], { stopped: boolean; operation: CELaunchStatus | null; recovered: boolean }>("stop_ce_for_game");

export const confirmTableWorking = callable<[appId: number, digest: string, sessionId: string, recordId: number], boolean>("confirm_table_working");
