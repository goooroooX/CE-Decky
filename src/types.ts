/** How a managed Cheat Engine artifact was obtained and what vouched for it. */
export interface CEProvenance {
  schema: number;
  /** `reviewed-url`, `cache`, or `rediscovered`. */
  source: string;
  verified_by: string;
  artifact_sha256: string;
  artifact_bytes: number;
  origin_host: string;
  helper_host: string | null;
  helper_format: string | null;
  reviewed_at: string;
  reviewed_subject: string;
  signature_subject: string | null;
  signature_common_name: string | null;
  signature_key_sha256: string | null;
  signature_digest: string | null;
  signature_note: string | null;
}

export interface CEStatus {
  configured: boolean;
  valid: boolean;
  executable: string | null;
  sha256: string | null;
  /** Version this exact executable declares, or `null` when it declares none. */
  version: string | null;
  reason: string | null;
  /** Whether this plugin installed it, as opposed to the user importing one. */
  managed: boolean;
  /** Recorded for a managed installation; `null` for an imported or pre-0.9.4 one. */
  provenance: CEProvenance | null;
}

export interface TableStatus {
  sha256: string;
  filename: string;
  size: number;
  table_version: string | null;
  has_lua: boolean;
  has_auto_assembler: boolean;
  has_embedded_files: boolean;
  /** A designed window the table carries; see `TableInspection.has_forms`. */
  has_forms?: boolean;
  executable_content: boolean;
  entry_count: number;
  blob_path: string;
  available: boolean;
  schema_version: number;
  origins: TableOrigin[];
  /**
   * When these bytes first arrived on this device, UTC.
   *
   * A downloaded table also carries the date in its origin's `retrieved_at`,
   * and that is the one a row shows, because it names the source it came from
   * in the same breath. This is what a table opened from a file has instead,
   * and it is what a table imported before arrival was tracked does not have.
   */
  imported_at?: string | null;
}

export interface TableOrigin {
  provider: string;
  artifact_id: string;
  topic_id: string;
  source_page: string;
  original_filename: string;
  retrieved_at: string;
  advertised_sha256: string | null;
  /**
   * Which table inside the artifact this is, for archives only.
   *
   * The advertised digest identifies the bytes the provider served, which for
   * an archive is the archive - and one archive can hold several tables, so it
   * only stands for this table when the import had no choice to make. Absent
   * from an origin recorded before this was tracked, and from a direct `.CT`.
   */
  member_path?: string;
  member_count?: number;
  /**
   * The release the provider advertised for these exact bytes, which is what
   * search offered them under. Absent from an origin recorded before this was
   * tracked, and from any import the provider gave no version for.
   */
  version?: string;
}

export interface ArchiveMember {
  path: string;
  size: number;
  packed_size: number | null;
  encrypted: boolean;
  format: string;
}

export interface TableSourceInspection {
  format: string;
  members: ArchiveMember[];
}

export interface TableControl {
  id: number | null;
  description: string;
  path: string[];
  variable_type: string | null;
  kind: "group" | "script" | "dropdown" | "value";
  group_header: boolean;
  has_assembler_script: boolean;
  dropdown_values: [string, string][];
  dropdown_read_only: boolean;
  /**
   * The record only attaches Cheat Engine to the game and changes nothing in
   * it. Table machinery rather than a cheat, so it is listed with the scripts.
   * Absent from an inspection made before this was recognised.
   */
  attach_only?: boolean;
}

export interface TableInspection {
  sha256: string;
  table_version: string | null;
  total_entries: number;
  has_lua: boolean;
  /**
   * A designed window stored in the table itself. Cheat Engine instantiates it
   * when the table is opened and its controls carry Lua handlers, so such a
   * table runs code and puts a window on screen while carrying no `<LuaScript>`
   * at all. Absent from an inspection made before this was recognised.
   */
  has_forms?: boolean;
  /**
   * Labels this had to remove an invisible or bidirectional character from, and
   * values it could not carry. Both used to refuse the whole table; measured
   * across 82 FearLess tables, four were lost that way. Absent from an
   * inspection made before either was counted.
   */
  sanitized_labels?: number;
  dropped_values?: number;
  /**
   * Whole value lists too large to carry.
   *
   * Counted apart from the values, because one of them is one picker gone from
   * a record rather than one value: the list this was added for holds 6508.
   */
  dropped_value_lists?: number;
  has_auto_assembler: boolean;
  embedded_files: number;
  process_candidates: string[];
  controls: TableControl[];
  ambiguous_record_ids: number[];
  unsupported_record_id_count: number;
}

export interface StartupPreference {
  record_id: number;
  active: boolean | null;
  value: string | null;
}

export interface ConfiguredValue {
  record_id: number;
  value: string;
}

export interface GameProfile {
  app_id: number;
  name: string;
  is_shortcut: boolean;
  table_sha256: string | null;
  target_process: string | null;
  execution_consent_sha256: string | null;
  startup: StartupPreference[];
  pinned: number[];
  previous_table_sha256: string | null;
  table_history: Record<string, { startup: StartupPreference[]; pinned: number[]; execution_consent: boolean; remembered: StartupPreference[]; configured_values?: ConfiguredValue[] }>;
  table_library: string[];
  autoload_enabled: boolean;
  remembered: StartupPreference[];
  /** Added in profile schema 5; absent only during an overlapping old-backend reload. */
  configured_values?: ConfiguredValue[];
}

export interface PreparedSession {
  session_id: string;
  app_id: number;
  ce_sha256: string;
  table_sha256: string;
  descriptor_path: string;
  descriptor_sha256: string;
  descriptor_md5: string;
  control_path: string;
  status_path: string;
  descriptor_windows_path: string;
  is_shortcut: boolean | null;
}

export interface RuntimeResult {
  generation: number;
  record_id: number | null;
  ok: boolean;
  active: boolean | null;
  value: string | null;
  error: string | null;
  /** Machine-readable bridge failure; absent from bridges before 0.9.0. */
  /**
   * `activation_rejected` means Cheat Engine accepted the change, finished, and
   * the record is still not in the requested state - for a script record, Cheat
   * Engine declining to run it.
   */
  error_code?: "record_missing" | "target_detached" | "address_list_unavailable" | "record_read_failed" | "mutation_failed" | "attach_failed" | "activation_rejected" | null;
}

export interface RuntimeStatus {
  session_id: string;
  app_id: number;
  ce_sha256: string;
  table_sha256: string;
  descriptor_sha256: string;
  heartbeat_ms: number;
  attached: boolean;
  target_process: string;
  opened_process_id: number;
  address_list_count?: number | null;
  /**
   * Whether the bridge has this session's exact table in Cheat Engine's
   * address list. Absent from a bridge that relied on Cheat Engine opening the
   * table named on its command line, which only happens once its main window
   * is shown - and CE Decky never lets that window map.
   */
  table_load_state?: "loaded" | "pending" | "failed" | null;
  /**
   * Which route opened that table. `approved` is Cheat Engine's own stream
   * overload, which takes the decision to run the table's Lua script as a
   * parameter, so the exact-SHA authorization given in Review is carried into
   * the load and Cheat Engine never asks. `prompted` is the older path form,
   * which asks, and whose question is a modal form nobody can reach over a
   * running game. Absent from a bridge that predates this and before a load has
   * been attempted.
   */
  table_load_route?: "approved" | "prompted" | null;
  table_load_error?: string | null;
  /**
   * `starting` while the bridge is still inside its own synchronous bootstrap,
   * `ready` once it has finished. A `starting` heartbeat is liveness and not
   * readiness: no table is open, nothing is attached and startup has not begun.
   * Absent from a bridge that published nothing at all until it was ready.
   */
  bridge_phase?: "starting" | "ready" | null;
  /**
   * How many times a Cheat Engine window had to be hidden again after the
   * initial suppression. Above zero means CE mapped a window over the running
   * game, which is what takes its audio and controller input.
   */
  window_suppressions?: number | null;
  /**
   * Sweeps that saw a Cheat Engine window and could not put it down. Cheat
   * Engine's own hide helper does not reach a form a table's Lua script
   * created, and a table's Lua script now runs, so a sweep can fail. It counts
   * attempts rather than windows: the sweep runs on a timer, so one window
   * nothing can hide raises it once per tick. Absent while every sweep
   * succeeded, and from a bridge that predates this.
   */
  unsuppressed_sweeps?: number | null;
  /**
   * Whether a Cheat Engine window is over the game right now. The count above
   * only climbs, so it cannot say the screen came back; this is the half that
   * can, and it is what the panel tells the user.
   */
  window_over_game?: boolean | null;
  /**
   * A Cheat Engine window hiding could not reach, dismissed because it was on
   * screen over the game. A message dialog is not one of the forms the sweep
   * enumerates, so it used to sit there unseen while the game lost its picture.
   * Closing it answers it on nobody's authority but ours, which is why the
   * count and the last caption are reported. Absent while nothing has been
   * dismissed, and from a bridge that predates this.
   */
  dialogs_dismissed?: number | null;
  last_dialog?: string | null;
  /**
   * Which call answers "is this window minimized" on this Cheat Engine, or
   * `unavailable`.
   *
   * A game that loses the foreground to Cheat Engine minimizes itself and stays
   * that way once the window is gone. Asking it to come back is only safe for a
   * window that really is minimized, because for a maximized one the same
   * request means "back to windowed size" - so without this the game is left
   * alone, and this is what says why. Absent from a bridge that predates it.
   */
  minimized_query?: string | null;
  /** Proven local-call routes and the last bounded invocation diagnostic. */
  focus_capability?: string | null;
  focus_error?: string | null;
  startup_active_ids?: number[];
  focus_discovery_attempts?: number | null;
  focus_candidates?: number | null;
  focus_attempts?: number | null;
  focus_successes?: number | null;
  focus_reason?: string | null;
  /** Which half of the restore capability failed, when one did. */
  restore_capability?: string | null;
  /** What the refused call said, when one refused. */
  restore_error?: string | null;
  /**
   * Absent from a bridge before 0.9.0.
   *
   * `failed_rolled_back` proves the game was put back; `failed_partial` says an
   * earlier startup action could not be proven undone and may still be applied.
   * A bridge older than this contract reports the undifferentiated `failed`.
   */
  startup_state?: "applied" | "pending" | "failed" | "failed_rolled_back" | "failed_partial" | null;
  /**
   * Monotonic startup progress, independent of the bounded result list.
   *
   * Every startup result carries generation 0 and the bridge keeps only the
   * most recent 128, so counting results stops growing on a large plan while
   * the bridge is still advancing. Absent from a bridge before this contract.
   */
  startup_completed?: number | null;
  startup_total?: number | null;
  results: RuntimeResult[];
  processes: [number, string][];
}

export interface RuntimeEnvelope {
  prepared: PreparedSession | null;
  status: RuntimeStatus | null;
  /** `null` when the mutable control log cannot be parsed, so no batch may be issued. */
  next_generation: number | null;
  status_age_ms: number | null;
  status_fresh: boolean;
  status_clock_skew: boolean;
  /** The heartbeat exists but does not parse: an unknown bridge state, not an absent one. */
  status_unreadable?: boolean;
  session_current: boolean;
  session_stale_reason: string | null;
  /** Set when this game's current session state cannot be read and needs repair. */
  session_state_reason?: string | null;
  connected: boolean;
  terminal_reason?: string | null;
}

export interface ArtifactResolution {
  provider: string;
  artifact_id: string;
  artifact_sha256: string;
  table_sha256: string;
}

export interface PluginStatus {
  table_compatibility?: { schema: number; entries: CompatibilityEvidence[]; reason: string | null };
  artifact_resolutions?: { schema: number; entries: ArtifactResolution[]; reason: string | null };
  version: string;
  user_home: string;
  managed_root: string;
  settings_dir: string;
  log_dir: string;
  log_file: string;
  sevenzip: string | null;
  ce: CEStatus;
  tables: TableStatus[];
  profiles: GameProfile[];
  config_state_reason: string | null;
  table_state_reason: string | null;
  table_catalog_errors?: Array<{ path: string; error: string }>;
  profile_state_reason: string | null;
  update?: PluginUpdateState;
  preferences?: PanelPreferences;
  features: Record<string, boolean>;
}

/** Whether the mascot is drawn on the home panel. Durable, and on by default. */
export interface PanelPreferences {
  mascot_visible: boolean;
}

/** One update this device is downloading, verifying or installing. */
export interface PluginUpdateOperation {
  operation_id: string;
  /**
   * `installing` is the point of no return, and the panel treats it as one.
   *
   * Decky replaces this plugin from there, which stops the backend that would
   * answer the next poll: nothing after that state is reported to this frontend
   * at all, and what happened is read from the durable record by the panel that
   * loads after the interface restarts.
   */
  state: "checking" | "downloading" | "installing" | "failed" | "cancelled";
  version: string | null;
  message: string;
  error: string | null;
}

/** What the last update did, as the backend that loaded afterwards read it. */
export interface PluginUpdateResult {
  version: string | null;
  ok: boolean;
  error: string | null;
  /** Where the verified archive was left for a manual install, when one failed. */
  archive_kept_at: string | null;
  at: number | null;
  restart_requested: boolean;
}

/**
 * A verified release this device is holding for a manual install.
 *
 * Its own fact rather than part of the last outcome: an attempt that fails
 * before it downloads anything is newer news and replaces that outcome, while
 * this file is still on the device and still the only thing a user can install
 * by hand. It is named only while the bytes at that path are provably the
 * release it says.
 */
export interface PluginUpdateRecovery {
  attempt: string | null;
  version: string | null;
  sha256: string;
  path: string;
}

export interface PluginUpdateState {
  current_version: string;
  auto_check: boolean;
  latest_version: string | null;
  update_available: boolean;
  checked_at: number | null;
  last_error: string | null;
  page_url: string;
  last_result: PluginUpdateResult | null;
  recovery: PluginUpdateRecovery | null;
  /** Whether this device has an interpreter the detached installer can run. */
  install_supported: boolean;
  checking: boolean;
  operation: PluginUpdateOperation | null;
}

export interface SelfTestCheck {
  name: string;
  ok: boolean;
  detail: string;
  blocking: boolean;
}

export interface SelfTestResult {
  ok: boolean;
  checks: SelfTestCheck[];
}

export interface ManagedCECapability {
  schema: number;
  mode: "managed_install" | "import_existing_only" | string;
  managed_install_available: boolean;
  release_manifest_loaded: boolean;
  network_download_enabled: boolean;
  native_extraction_enabled: boolean;
  reason: string;
  release: {
    visible_version: string;
    artifact_filename: string;
    sha256: string;
    size: number;
    reviewed_at: string;
    rediscovery_available: boolean;
  } | null;
  operation: ManagedCEInstallStatus | null;
}

export interface ManagedCEInstallStatus {
  operation_id: string;
  state: "downloading" | "extracting" | "verifying" | "completed" | "failed" | "cancelled" | string;
  progress: number | null;
  message: string;
  error: string | null;
  installed: { executable: string; root: string; sha256: string; size: number } | null;
  /** Present once acquisition has settled which route produced the artifact. */
  provenance: CEProvenance | null;
}

export interface ManagedCECompletion {
  executable: string;
  root: string;
  sha256: string;
  size: number;
  completed_now: boolean;
}

export interface ProtonToolIdentity {
  tool_id: string;
  name: string;
  path: string;
  proton_sha256: string;
  source: string;
}

export interface GameContainerObservation {
  app_id: number;
  running: boolean;
  pids: number[];
  compat_data_path: string | null;
  steam_client_install_path: string | null;
  wine_prefix: string | null;
  compat_tool_paths: string[];
  /** Windows .exe basenames this game's own processes are running right now. */
  windows_executables?: string[];
  /** The executable Steam asked Proton to run for this game, observed in its own process table. */
  launch_executable?: string | null;
  conflicting_compat_data_paths: string[];
  conflicting_steam_client_install_paths: string[];
  conflicting_wine_prefixes: string[];
  scanned: number;
  reason: string | null;
}

export interface CompatDataResolution {
  app_id: number;
  state: "resolved" | "missing" | "ambiguous" | "unsafe" | string;
  compat_data_path: string | null;
  candidates: string[];
  unsafe_paths: string[];
}

export interface CELaunchStatus {
  operation_id: string;
  mode: "self_test" | "attached" | string;
  app_id: number | null;
  session_id: string;
  state: "starting" | "running" | "connected" | "stopped" | "failed" | "cancelled" | string;
  message: string;
  error: string | null;
  started_at: number;
  plan: {
    mode: string;
    app_id: number | null;
    tool_id: string;
    tool_name: string;
    tool_path: string;
    proton_sha256: string;
    verb: string;
    compat_data_path: string;
    steam_client_install_path: string;
    executable: string;
    ce_sha256: string;
    argv: string[];
    env_overrides: string[][];
    descriptor_windows_path: string;
    descriptor_sha256: string;
    table_windows_path: string;
    table_sha256: string;
    session_id: string;
  };
  game: GameContainerObservation | null;
  bridge: {
    session_id: string;
    descriptor_sha256: string;
    table_sha256: string;
    attached: boolean;
    target_process: string;
    opened_process_id: number;
    process_count: number;
    heartbeat_ms: number;
  } | null;
  pid: number | null;
  pgid: number | null;
  exit_code: number | null;
  log_tail: string;
}

export interface OwnedLaunchRecord {
  schema: number;
  app_id: number;
  session_id: string;
  pid: number;
  pgid: number;
  tool_id: string;
  executable: string;
  descriptor_windows_path: string;
  descriptor_sha256: string;
  started_at: number;
}

export interface CELaunchCapability {
  schema: number;
  modes: string[];
  self_test_prefix: string;
  operations: CELaunchStatus[];
  game: GameContainerObservation | null;
  compat_data: CompatDataResolution | null;
  proton_tools: ProtonToolIdentity[];
  reason: string | null;
  observed_proton_tool: ProtonToolIdentity | null;
  observed_proton_reason: string | null;
  ce_executable_sha256: string | null;
  ce_ready: boolean;
  recovered: OwnedLaunchRecord | null;
  recovery_error: string | null;
  /** Set when a recovered Cheat Engine is running a bridge this build replaced. */
  recovered_bridge_mismatch?: string | null;
  /** Every game currently holding a CE Decky-owned Cheat Engine, at launcher scope. */
  owned_launch_owners: Array<{ app_id: number; state: string; recovered: boolean }>;
  /** Set when that inventory is incomplete, so the backend still treats ownership as held. */
  ownership_state_error?: string | null;
}

export interface ProviderDefinition {
  provider: string;
  provider_display_name: string;
  priority: number;
  enabled_by_default: boolean;
  adapter_kind: string;
  network_state: "target_gate" | string;
  /** A download from this source begins with a wait it serves out itself. */
  serves_download_wait?: boolean;
}

export interface CatalogResult {
  search_id?: string;
  provider: string;
  provider_display_name: string;
  topic_id: string;
  artifact_id: string;
  table_title: string;
  filename: string;
  version: string | null;
  size_bytes: number | null;
  source_page: string;
  download_mode: string;
  match_score: number;
  provider_rank: number;
  author: string | null;
  posted_at: string | null;
  download_count: number | null;
  notes: string | null;
  stale: boolean;
  advertised_sha256: string | null;
  password_required: boolean;
}

export interface CatalogSearchOutcome {
  search_id?: string;
  results: CatalogResult[];
  failures: Array<{ provider: string; error: string; handoff_url?: string | null }>;
  /** Every source that was searched, including ones that returned nothing. */
  sources?: ProviderSearchSummary[];
  stale: boolean;
}

/** What one source is doing while a search is still running. */
export interface TableSearchSource {
  provider: string;
  name: string;
  state: "running" | "done" | "failed" | "off";
  /** What it is doing right now, in the words the panel shows. */
  stage: string | null;
}

/**
 * The search running right now, for the screen that is waiting on it.
 *
 * A search is tens of seconds of somebody else's network, and the panel could
 * say only how long it had been waiting. This is read separately from the
 * search itself, because the search answers once and that is the moment this
 * stops being of any use.
 */
export interface TableSearchProgress {
  schema: number;
  running: boolean;
  elapsed_ms: number;
  sources: TableSearchSource[];
}

export interface AcquisitionStatus {
  resolved_table_sha256?: string | null;
  failure_cause?: BlockedTableCause | null;
  acquisition_id: string;
  provider: string;
  artifact_id: string;
  filename: string;
  state: "waiting_provider" | "downloading" | "browser_handoff" | "ready_to_import" | "needs_selection" | "imported" | "failed" | "cancelled";
  error: string | null;
  bytes_received: number;
  expected_bytes: number | null;
  provider_wait_seconds: number | null;
  /**
   * Why the acquisition is waiting: `preparing` is the provider's own countdown
   * before it hands over a link, `rate_limited` is the provider refusing for
   * now and being given time to stop, and `busy` is a provider that serves one
   * download at a time saying it is already serving this client another. None
   * of the three is the same thing to be told, and `busy` restarts a countdown
   * that has just run out, which reads as the dialog beginning again for no
   * reason at all unless it says why.
   */
  provider_wait_reason?: "preparing" | "rate_limited" | "busy" | null;
  source_page: string | null;
  inspection: TableSourceInspection | null;
  imported: TableStatus | null;
  execution_consent: false | null;
  /** The bytes arrived intact and are still not a usable Cheat Engine table. */
  artifact_rejected?: boolean;
}


export interface ProviderSearchPlan {
  aliases: string[];
  queries: string[];
  providers: ProviderDefinition[];
  /** Provider IDs left out of the plan because the user switched them off. */
  switched_off?: string[];
  network_enabled: boolean;
  requires_target_validation: true;
}

export interface ProviderCandidateInput {
  title: string;
  provider_trust?: number;
  platform_tags?: string[];
  has_artifact?: boolean;
  provider_id?: string | null;
  release_year?: number | null;
}

export interface ProviderMatchBreakdown {
  confidence: number;
  exact: number;
  chars: number;
  tokens: number;
  trigrams: number;
  prefix: number;
  acronym: number;
  numeric_conflict: boolean;
  request_penalty: number;
  platform_bonus: number;
  matched_query: string;
  matched_candidate: string;
  year_conflict: boolean;
  year_uncertain: boolean;
  qualifier_conflict: boolean;
}

export interface ProviderMatchDecision {
  action: "auto" | "ask" | "reject";
  reason: string;
  ranked: Array<{
    candidate: {
      title: string;
      provider_trust: number;
      platform_tags: string[];
      has_artifact: boolean;
      provider_id: string | null;
      release_year: number | null;
    };
    match: ProviderMatchBreakdown;
  }>;
}

export interface ProviderDiagnosticsEntry {
  state: string;
  /** Historical state for a provider absent from the current registry. */
  retired?: boolean;
  counters: {
    searches: number;
    results: number;
    downloads_succeeded: number;
    downloads_failed: number;
    bytes_downloaded: number;
    /**
     * A rate limit an artifact download waited out and then recovered from. It
     * is not a failed download and is never counted as one, but a provider that
     * throttles every transfer is invisible without it. Absent from diagnostics
     * written before it was counted.
     */
    downloads_throttled?: number;
    errors: number;
    /**
     * Rows this provider served because another provider's page named an exact
     * page on it. Counted apart from `searches`, because no search of this
     * provider ran. Absent from diagnostics written before it was counted.
     */
    linked_reads?: number;
    /**
     * What a provider published that could not be read. `parse_degraded` lost
     * description or a single row while tables stayed obtainable;
     * `parse_failed` is a page that could not be read at all. Absent from
     * diagnostics written before they were counted.
     */
    parse_degraded?: number;
    parse_failed?: number;
  };
  last_http_status: number | null;
  last_latency_ms: number | null;
  last_error: string | null;
  cooldown_until_epoch_s: number;
  /** How long the last recovered rate limit cost, in seconds. */
  last_throttle_wait_s?: number | null;
}

export interface ProviderDiagnosticsSnapshot {
  schema: number;
  providers: Record<string, ProviderDiagnosticsEntry>;
}

/**
 * One table source the user can switch, and what it has actually been doing.
 *
 * The registry, the user's own choice and the provider's diagnostics arrive
 * together because the screen offering the switch is the screen that has to
 * justify it: a source is switched off after it has been seen failing, and that
 * evidence is the counters below.
 */
export interface ProviderSourceStatus {
  provider: string;
  provider_display_name: string;
  priority: number;
  /** `search` runs a query of its own; `linked_source` is only ever followed. */
  discovery: string;
  /**
   * Whether another source's page can name an exact page on this one and have
   * it read. Not implied by `discovery`: GitHub is searched and is also named
   * this way, while The Cheat Script and VGTimes are searched and named by
   * nobody, so the screen cannot claim both routes for every searched source.
   */
  linked_target: boolean;
  enabled: boolean;
  /**
   * Null when this source has never been asked for anything. Nothing recorded
   * and everything recorded as zero are different facts, and a row that shows
   * them alike cannot say which sources have actually been tried.
   */
  state: string | null;
  counters: ProviderDiagnosticsEntry["counters"] | null;
  last_error: string | null;
  last_http_status: number | null;
  last_latency_ms: number | null;
  last_throttle_wait_s?: number | null;
  /** Whole seconds the provider itself asked to be left alone for, or 0. */
  cooldown_seconds: number;
}

export interface ProviderSourcesSnapshot {
  schema: number;
  sources: ProviderSourceStatus[];
  enabled_count: number;
  total: number;
  updated_at: number | null;
  /** Why the record of switched-off sources could not be read, when it could not. */
  selection_reason: string | null;
  /** Why the provider counters are missing, when they are. */
  diagnostics_reason: string | null;
}

export interface ManagedDirectory {
  key: string;
  label: string;
  path: string;
  purpose: string;
  exists: boolean;
  file_count: number;
  total_bytes: number;
  truncated: boolean;
  error: string | null;
}

export interface RemovalReadiness {
  /** What CE Decky has put on disk, directory by directory. */
  directories: ManagedDirectory[];
  profiles_total: number;
  current_session_app_ids: number[];
  /** True while a Cheat Engine process CE Decky owns is still executing. */
  live_owned_launch: boolean;
  session_errors: Array<{ app_id: number; error: string }>;
  session_corrupt_entries: number;
  profile_state_error: string | null;
  session_inventory_error: string | null;
  blockers: string[];
  can_delete_managed_data: boolean;
  managed_root: string;
  requires_target_validation: boolean;
}

/** The three scopes the panel offers for deleting plugin data on disk. */
export type ManagedDataScope = "cache" | "setup" | "all";

export interface ManagedDataRemoval {
  key: string;
  label: string;
  removed_files: number;
  removed_bytes: number;
  error: string | null;
}

export interface ManagedDataDeletion {
  scope: ManagedDataScope;
  deleted: ManagedDataRemoval[];
  /** Directories that could not be cleared. The rest of the deletion still happened. */
  failed: ManagedDataRemoval[];
  /**
   * The report re-read after deletion, so the panel never shows stale sizes.
   *
   * Null when that read failed. It happens after the files are already gone, so
   * raising there would report a committed deletion as one that never ran.
   */
  readiness: RemovalReadiness | null;
  /** Why the post-deletion report is missing, when it is. */
  readiness_error?: string | null;
}

export interface DiagnosticsSnapshot {
  uptime_s: number;
  log_path: string;
  version: string;
  storage: { tables: number; table_bytes: number; profiles: number };
  providers: Record<string, ProviderDiagnosticsEntry>;
  /** The sources the user switched off, so a counter of zero can be read correctly. */
  provider_selection?: {
    schema: number;
    disabled: string[];
    updated_at: number | null;
    reason: string | null;
  };
  fearless_index: FearlessIndexStatus;
  sessions: SessionInventory;
  config_state_error: string | null;
  table_state_error: string | null;
  table_catalog_errors: Array<{ path: string; error: string }>;
  profile_state_error: string | null;
  provider_state_error: string | null;
  session_state_error: string | null;
  capabilities: Record<string, boolean>;
}

/**
 * Where the collected support archive was written, and what it does not hold.
 *
 * `notes` is the part worth reading: every file the collector could not take
 * appears there with its reason, and an unreadable state file is very often the
 * defect being reported rather than a flaw in the collection.
 */
export interface SupportBundleResult {
  schema: number;
  /** Absolute path of the archive, which is what the user is shown. */
  path: string;
  filename: string;
  size_bytes: number;
  member_count: number;
  uncompressed_bytes: number;
  /**
   * Why a member is missing, or why an included one holds less than the whole.
   *
   * `kind` separates those: `omitted` is the only one that means the archive
   * does not carry something it could have. `truncated` and `partial` are both
   * in the archive and both readable, and counting either as something that
   * could not be collected made a healthy bundle look damaged. `partial` was
   * added to the collector without being added here, and the panel then counted
   * it, which is exactly the reading this field exists to prevent. `absent` is
   * nothing to collect: a device that has never launched Cheat Engine has no
   * launch logs and a plugin just reloaded has no log file yet, and a healthy
   * Steam Deck reported both of those as items it could not collect.
   */
  notes: Array<{ member: string; reason: string; kind: "omitted" | "truncated" | "partial" | "absent" } & Record<string, unknown>>;
  /** Older bundles removed to keep the home directory from filling up. */
  removed_older_bundles: string[];
}

/**
 * What one installed game's own files hold, read only.
 *
 * The weakest of the three sources of a target process, and the only one that
 * exists before the game has ever run: a table that names no process, for a
 * Steam game that is not running, otherwise leaves the Review screen with no
 * candidate and its one press disabled. It says what is there and how deep it
 * is; nothing in it claims which executable owns the game's memory.
 */
export interface GameExecutable {
  name: string;
  /** Where it sits under the install directory; `""` is the root itself. */
  directory: string;
  depth: number;
  size_bytes: number;
  /** Steam itself starts this for this game, rather than a walk having found it. */
  declared: boolean;
  /** What Steam calls this launch option, where it calls it anything. */
  description: string | null;
}

export interface GameExecutableListing {
  schema: number;
  app_id: number | null;
  install_dir: string | null;
  executables: GameExecutable[];
  /** The walk stopped at its own bound; what came back is still what it found. */
  truncated: boolean;
  /** Where this list came from: Steam's own record, or a walk of the folder. */
  source: "steam" | "files" | null;
  /** Why Steam's own record did not answer, when the walk is what did. */
  declared_reason: string | null;
  /** Why there is nothing, said in the user's terms. `null` when there is. */
  reason: string | null;
  /**
   * The same answer in one word, for a screen deciding what to offer.
   *
   * `no_windows_executable` is the one that means something rather than
   * nothing: the walk completed and this game holds no Windows program at all,
   * which is what a native Linux build looks like and what Cheat Engine has
   * nothing to attach to.
   */
  cause: "libraries_unreadable" | "not_installed" | "ambiguous_library" | "too_many_files" | "unreadable_files" | "executable_too_deep" | "no_windows_executable" | null;
}

/**
 * Which of the account's library entries this device actually holds.
 *
 * Steam's library is an account's rather than a machine's, so a second device
 * lists titles whose files are on the first one. These come from this device's
 * own manifests and its own shortcut store.
 */
export interface LocalLibrary {
  schema: number;
  /** Steam AppIDs whose manifest here says they are fully installed. */
  steam_app_ids: number[];
  /**
   * Of those, the ones Steam declares no way to start: Proton, the runtimes.
   *
   * Not what Steam calls them. It types Half-Life 2's three episodes `Tool` as
   * well, and those are games people cheat in.
   */
  unstartable_app_ids: number[];
  /** Non-Steam shortcuts this device's own `shortcuts.vdf` holds. */
  shortcut_app_ids: number[];
  /** Why the installed list could not be read; `null` when it was. */
  reason: string | null;
  /** Why the shortcut store could not be read; `null` when it was. */
  shortcuts_reason: string | null;
}

export interface SessionInventoryApp {
  app_id: number;
  session_count: number;
  current_session_id: string | null;
  current_error: string | null;
  corrupt_entries: number;
}

export interface SessionInventory {
  apps: SessionInventoryApp[];
  total_sessions: number;
  errors: Array<{ path: string; error: string }>;
}

export interface FearlessIndexStatus {
  status: string;
  error: string | null;
  indexed_pages: number;
  total_pages: number | null;
  indexed_topics: number;
  retry_after_seconds: number | null;
  /** How old a cached listing page may get before it is read again. */
  refresh_age_seconds: number;
  /** When the least recently fetched page was read, once every page is held. */
  fully_refreshed_at: number | null;
  /** Pages the next background pass owes: never fetched, or aged out. */
  stale_pages: number;
  last_refresh_at: number | null;
  last_refresh_pages: number;
}

export interface ProviderSearchSummary extends Partial<FearlessIndexStatus> {
  provider: string;
  provider_display_name: string;
  results: number;
  status: string;
  error: string | null;
}

export interface RunningAppIdObservation {
  available: boolean;
  app_ids: number[];
  scanned: number;
  reason: string | null;
}

/**
 * One exact table recorded as not working, and what happened when it was tried.
 *
 * Keyed by content, because that is the only identity that survives a provider
 * renaming a file or serving it from a different post. `game_version` is what
 * the game's own executable declared at the time: a table stops working because
 * the game was updated, so the build it was tried against is what dates the
 * record.
 */
/**
 * One readable piece of a table's own code.
 *
 * The index carries no body text at all: one table on this device holds 529 KiB
 * of scripts across 99 records, so the list of what is inside and the reading of
 * any one of them are two separate calls.
 */
export interface TableCodeSection {
  id: string;
  kind: "lua" | "auto_assembler" | "form" | "embedded_file";
  /** The cheat this script belongs to, or what the section is. */
  title: string;
  /** The groups the record sits under, outermost first. */
  path: string[];
  bytes: number;
  lines: number;
  truncated: boolean;
  /** False for an embedded payload, whose bytes are deliberately never sent. */
  readable: boolean;
}

export interface TableCodeIndex {
  schema: number;
  sha256: string;
  size: number;
  sections: TableCodeSection[];
  totals: Record<string, number>;
  /** Every cheat the table declares, groups included, whether or not it runs code. */
  records: number;
  omitted_sections: number;
}

export interface TableCodeBody {
  schema: number;
  id: string;
  kind: TableCodeSection["kind"];
  title: string;
  path: string[];
  lines: string[];
  total_lines: number;
  truncated: boolean;
  /** A line carried an invisible or reordering character, which was removed. */
  sanitized: boolean;
  readable: boolean;
}

/**
 * Why a table is in the not-working record, as the backend recorded it.
 *
 * Four different things, and a user deciding what to do about a row acts on
 * each of them differently: `refused` ran and the cheat came straight back off,
 * `unusable` was never a table at all, `encrypted` is an archive only 7-Zip
 * opens with every member of it locked, and `gone` is the source saying it no
 * longer has the file. `unknown` is the floor for a cause that cannot be read.
 */
export type BlockedTableCause = "refused" | "unusable" | "encrypted" | "gone" | "unknown";

export interface BlockedTable {
  /**
   * What this record is cleared by.
   *
   * The digest where there is one. A row whose file the source says it no
   * longer has never produced bytes, so it is keyed by the provider row itself
   * and carries no digest at all.
   */
  key: string;
  sha256: string | null;
  reason: string;
  filename: string | null;
  app_id: number | null;
  game_name: string | null;
  game_version: string | null;
  /**
   * The release the source advertised for these exact bytes.
   *
   * What tells this record apart from the record of the revision beside it: one
   * post carries every revision of a table and its attachments share a filename
   * and a title, and the file itself is often gone from the device by the time
   * this list is read. `null` where no source stated one; the `.CT` file's own
   * `CheatEngineTableVersion` is never used for it, because that is the version
   * of Cheat Engine's table format rather than of the table.
   */
  table_version: string | null;
  /** Seconds since the epoch. */
  recorded_at: number;
  /**
   * `provider:artifact_id` for every provider row these bytes came from.
   *
   * A search result always has a provider and an artifact ID and only sometimes
   * advertises a content digest, so this is what lets the mark grey the row it
   * was recorded from. Empty for a local import, and absent from an entry
   * written before this was tracked.
   */
  origins?: string[];
  /** Absent from an entry written before the cause was recorded. */
  cause?: BlockedTableCause;
}

export interface BlockedTableList {
  schema: number;
  /** Non-null when the record could not be read; the list is then empty. */
  reason: string | null;
  tables: BlockedTable[];
}


export interface CompatibilityEvidence {
  app_id: number;
  table_sha256: string;
  /** The process this evidence is about. A row without one is not readable evidence and is never published. */
  target_process: string;
  pe_version: string | null;
  steam_build_id: string | null;
  last_working_at: number;
  invalidated: boolean;
  state: "matching" | "retest" | "unknown";
}
