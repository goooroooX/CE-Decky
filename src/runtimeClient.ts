import { logUiWarning } from "./supportLog";
import { confirmTableWorking, getRuntimeStatus, writeRuntimeCommands } from "./api";
import type { RuntimeEnvelope, RuntimeResult } from "./types";
import { describeError, leadWithCause } from "./errors";
import { isPreCommitRefusal } from "./durableWrite";
import { monotonicNow } from "./elapsed";

export interface RuntimeCommandSpec {
  kind: string;
  record_id?: number | null;
  value?: string | null;
  target_pid?: number | null;
}

export interface RuntimeBatchResult {
  envelope: RuntimeEnvelope;
  results: RuntimeResult[];
}

export interface DeactivateAllResult {
  queried: number;
  active: number;
  deactivated: number;
  /** The exact records this call switched off, so the caller remembers only those. */
  deactivatedIds: readonly number[];
  envelope: RuntimeEnvelope;
}

export interface RuntimeDesiredState {
  record_id: number;
  active: boolean | null;
  value: string | null;
  /** Static table path used only to preserve parent-before-child mutation order. */
  path?: readonly string[];
  /** Human name for this record, used in any message the user has to read. */
  label?: string;
  /**
   * The two keys a switch record's toggle writes, when this record is one.
   *
   * Present, `active` decides the value and `value` is ignored: the record's
   * list is the switch, so on writes one key and off writes the other. Off has
   * to write: releasing the freeze on a record still holding its on value
   * leaves the cheat running in the game under a switch that says it is off.
   */
  switch_values?: { on: string; off: string };
}

export class RuntimeOperationError extends Error {
  envelope: RuntimeEnvelope | null;
  /**
   * Cheat Engine ran the cheat and it came straight back off.
   *
   * This is the one runtime failure that is about the table rather than about
   * the session, so it is the one a caller can act on: it is what a table
   * written for a different build of the game does, every time, and the panel
   * offers to stop using that table because of it.
   */
  tableRefused: boolean;

  constructor(message: string, envelope: RuntimeEnvelope | null = null, tableRefused = false) {
    super(message);
    this.name = "RuntimeOperationError";
    this.envelope = envelope;
    this.tableRefused = tableRefused;
  }
}

/** A command was accepted durably but its result has not arrived yet. */
export class RuntimeOutcomeUnknownError extends RuntimeOperationError {
  readonly pendingGenerations: readonly number[];

  constructor(message: string, envelope: RuntimeEnvelope, pendingGenerations: readonly number[]) {
    super(message, envelope);
    this.name = "RuntimeOutcomeUnknownError";
    this.pendingGenerations = pendingGenerations;
  }
}

/**
 * How many controls one live operation may address.
 *
 * The static inspector accepts up to 100k entries, which is the right ceiling
 * for a parser but not for a Game Mode workflow: each 64-command batch is a
 * status read, a durable write and acknowledgement polling, so the parser
 * ceiling alone is over 1500 sequential round-trips for a single snapshot. This
 * is the product limit, chosen so a full refresh stays inside a controller
 * latency budget rather than taking minutes.
 */
export const MAX_LIVE_CONTROLS = 512;

const ACK_POLL_INTERVAL_MS = 125;
// The bridge may intentionally hold an asynchronous Auto Assembler activation
// pending for up to ten seconds. Wait beyond that bounded bridge deadline so a
// valid late acknowledgement is not mislabeled as an unknown outcome.
const ACK_POLL_ATTEMPTS = 96;
// Cheat Engine builds a script's records after the script runs, so the first
// query after an activation can legitimately still find nothing.
const MATERIALIZATION_ATTEMPTS = 6;
const MATERIALIZATION_DELAY_MS = 250;
const COMMAND_BATCH_SIZE = 64;

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

// How long a press waits for a bridge that is busy rather than gone before it
// reports the silence. The bridge's own bounded allowance for an asynchronous
// activation is ten seconds and a synchronous one blocks its timer for as long
// as Cheat Engine takes to assemble and inject, so a press arriving during
// either used to be refused outright three seconds in.
const BUSY_BRIDGE_WAIT_MS = 12_000;
const BUSY_BRIDGE_POLL_MS = 250;

/**
 * The current envelope, having given a busy bridge a bounded moment to answer.
 *
 * Nothing here is fail-open: it returns whatever it last read, and the caller
 * still refuses to issue a command against an envelope that is not connected.
 * The wait only exists so that the refusal describes a bridge that is actually
 * gone rather than one that was in the middle of running a script.
 */
async function readConnectedEnvelope(appId: number): Promise<RuntimeEnvelope> {
  const deadline = monotonicNow() + BUSY_BRIDGE_WAIT_MS;
  let envelope = await getRuntimeStatus(appId);
  while (!envelope.connected && bridgeMayStillBeWorking(envelope, appId) && monotonicNow() < deadline) {
    await delay(BUSY_BRIDGE_POLL_MS);
    envelope = await getRuntimeStatus(appId);
  }
  return envelope;
}

/**
 * Why this envelope is not a connected bridge, in the user's terms, or `null`.
 *
 * There are four different things a disconnected bridge can be and the screen
 * used to say the same sentence for all of them: no session at all, a session
 * whose state cannot be read, a Cheat Engine that has exited, and a Cheat
 * Engine that is alive and has simply not written a heartbeat for a moment.
 * Only the third is a session that is over, and only the fourth is worth
 * waiting for, so a reader who is told "disconnected or its heartbeat is stale"
 * cannot tell whether to start the game again or to press the same button once
 * more. The backend already distinguishes all four; this is what carries that
 * distinction to the person reading it.
 */
function disconnectedReason(envelope: RuntimeEnvelope): string | null {
  if (envelope.status_unreadable || envelope.session_state_reason) {
    return `This game's runtime session state cannot be read: ${envelope.session_state_reason ?? "the bridge's own status file does not parse"}. Repair it under Advanced.`;
  }
  if (!envelope.prepared) {
    return envelope.session_stale_reason
      ?? "No Cheat Engine session is prepared for this game. Start Cheat Engine for this table first.";
  }
  if (envelope.terminal_reason === "owned_bridge_process_exited") {
    return "Cheat Engine has exited, so this table is no longer loaded. Start it again for this game.";
  }
  if (!envelope.session_current) {
    return envelope.session_stale_reason ?? "This game's prepared session is no longer the current one.";
  }
  if (!envelope.status) {
    return "Cheat Engine has not reported its state for this session yet.";
  }
  if (envelope.status_clock_skew) {
    return "Cheat Engine's last heartbeat is dated in the future, so its age cannot be judged. This device's clock changed while the session was running.";
  }
  if (!envelope.status_fresh) {
    const seconds = typeof envelope.status_age_ms === "number" ? Math.round(envelope.status_age_ms / 1000) : null;
    return `Cheat Engine has not answered for ${seconds === null ? "some time" : `${seconds}s`}. It stops answering while it runs the table's own script, so this is usually worth one more press; if it keeps saying this, Cheat Engine is no longer running.`;
  }
  if (!envelope.connected) return "The resident bridge is not connected for this session.";
  return null;
}

/**
 * Whether the bridge is merely not answering right now, rather than gone.
 *
 * Cheat Engine's heartbeat is written from a timer on the same thread that runs
 * a table's Auto Assembler and Lua, so a script that takes longer than the
 * heartbeat's own three seconds to assemble and inject stops the heartbeat for
 * exactly as long as it runs. That is a bridge doing what it was asked to do,
 * and treating it as a bridge that has gone is what turned "switch this cheat
 * on" into "Resident bridge is disconnected" on tables whose scripts are large.
 *
 * This never authorizes a write: a command is still only issued against a
 * connected envelope. It authorizes waiting for one.
 */
function bridgeMayStillBeWorking(envelope: RuntimeEnvelope, appId: number): boolean {
  return Boolean(
    // Only ever a reason to wait, never a way past a check: an envelope that is
    // connected is judged by the identity comparison in full, exactly as
    // before.
    !envelope.connected
    && envelope.prepared
    && envelope.status
    && envelope.session_current
    && !envelope.status_unreadable
    && !envelope.session_state_reason
    && !envelope.status_clock_skew
    && !envelope.terminal_reason
    && envelope.prepared.app_id === appId
    && envelope.status.app_id === appId
    && envelope.prepared.session_id === envelope.status.session_id
    && envelope.prepared.table_sha256 === envelope.status.table_sha256
    && envelope.prepared.ce_sha256 === envelope.status.ce_sha256
    && envelope.prepared.descriptor_sha256 === envelope.status.descriptor_sha256,
  );
}

function assertConnected(envelope: RuntimeEnvelope, appId: number): void {
  if (!envelope.connected || !envelope.prepared || !envelope.status) {
    throw new Error(disconnectedReason(envelope) ?? "Resident bridge is disconnected or its heartbeat is stale.");
  }
  if (!envelope.session_current || envelope.prepared.app_id !== appId || envelope.status.app_id !== appId) {
    throw new Error("Runtime session identity is stale or does not match the selected AppID.");
  }
  if (
    envelope.prepared.session_id !== envelope.status.session_id
    || envelope.prepared.table_sha256 !== envelope.status.table_sha256
    || envelope.prepared.ce_sha256 !== envelope.status.ce_sha256
    || envelope.prepared.descriptor_sha256 !== envelope.status.descriptor_sha256
  ) {
    throw new Error("Runtime bridge identity does not match the prepared exact session.");
  }
}

/**
 * The same exact session, still. `allowBusy` waits out a silent heartbeat.
 *
 * Identity is compared either way: what `allowBusy` permits is a bridge that
 * has not written for a moment, never a different session, a different table or
 * a Cheat Engine that is proven gone.
 */
function assertSameSession(before: RuntimeEnvelope, after: RuntimeEnvelope, appId: number, allowBusy = false): void {
  if (!(allowBusy && bridgeMayStillBeWorking(after, appId))) {
    try {
      assertConnected(after, appId);
    } catch (cause) {
      throw new RuntimeOperationError(
        describeError(cause),
        after,
      );
    }
  }
  if (
    before.prepared?.session_id !== after.prepared?.session_id
    || before.prepared?.table_sha256 !== after.prepared?.table_sha256
    || before.prepared?.ce_sha256 !== after.prepared?.ce_sha256
    || before.prepared?.descriptor_sha256 !== after.prepared?.descriptor_sha256
  ) {
    throw new RuntimeOperationError(
      "Runtime session changed while waiting for bridge acknowledgement.",
      after,
    );
  }
}

function resultsForCommands(
  envelope: RuntimeEnvelope,
  commands: ReadonlyArray<{ generation: number; record_id?: number | null }>,
): RuntimeResult[] | null {
  const results = envelope.status?.results ?? [];
  const byGeneration = new Map<number, RuntimeResult>();
  for (const result of results) byGeneration.set(result.generation, result);
  const selected: RuntimeResult[] = [];
  for (const command of commands) {
    const result = byGeneration.get(command.generation);
    if (!result) return null;
    const expectedRecordId = command.record_id ?? null;
    if (result.record_id !== expectedRecordId) {
      throw new RuntimeOperationError(
        `Resident bridge acknowledgement identity mismatch for generation ${command.generation}.`,
        envelope,
      );
    }
    selected.push(result);
  }
  return selected;
}

export async function sendRuntimeBatchAndWait(
  appId: number,
  specs: readonly RuntimeCommandSpec[],
  expectedEnvelope?: RuntimeEnvelope,
): Promise<RuntimeBatchResult> {
  if (specs.length < 1) throw new Error("Runtime command batch is empty.");
  if (specs.length > COMMAND_BATCH_SIZE) throw new Error(`Runtime command batch exceeds ${COMMAND_BATCH_SIZE} commands.`);

  let before: RuntimeEnvelope;
  try {
    before = await readConnectedEnvelope(appId);
  } catch (cause) {
    throw new RuntimeOperationError(
      describeError(cause),
      null,
    );
  }
  try {
    assertConnected(before, appId);
    if (expectedEnvelope) assertSameSession(expectedEnvelope, before, appId);
  } catch (cause) {
    if (cause instanceof RuntimeOperationError) throw cause;
    throw new RuntimeOperationError(
      describeError(cause),
      before,
    );
  }
  const start = before.next_generation;
  if (typeof start !== "number") {
    // The control log this generation would extend cannot be parsed, so there
    // is no generation to claim; repair the session state instead of guessing.
    throw new RuntimeOperationError(
      before.session_state_reason ?? "This game's runtime session state cannot be read.",
      before,
    );
  }
  const commands = specs.map((spec, index) => ({ generation: start + index, ...spec }));
  let receipt: Awaited<ReturnType<typeof writeRuntimeCommands>>;
  try {
    receipt = await writeRuntimeCommands(appId, commands);
  } catch (cause) {
    // The backend validates and then atomically commits the generation before
    // it returns, so a rejection here can mean either "refused before commit"
    // or "committed and the answer was lost". Reporting both as a definite
    // failure told the user nothing had happened when the game may already
    // have changed. A Python traceback is proof the backend itself rejected
    // the batch - except the one it raises after the control file has already
    // been replaced, which Cheat Engine is free to read and execute from that
    // moment on. That and transport loss both leave the outcome unknown.
    if (isPreCommitRefusal(cause)) {
      throw new RuntimeOperationError(describeError(cause), before);
    }
    throw new RuntimeOutcomeUnknownError(
      `${describeError(cause)} The command may already have been accepted; refresh this game's runtime state before retrying.`,
      before,
      commands.map((command) => command.generation),
    );
  }
  if (!receipt.ok || receipt.count !== commands.length || receipt.next_generation !== start + commands.length) {
    throw new RuntimeOperationError(
      "Runtime command write receipt does not match the requested generation batch.",
      before,
    );
  }
  const generations = commands.map((command) => command.generation);
  let lastEnvelope = before;

  for (let attempt = 0; attempt < ACK_POLL_ATTEMPTS; attempt += 1) {
    let envelope: RuntimeEnvelope;
    try {
      envelope = await getRuntimeStatus(appId);
    } catch (cause) {
      // The batch was accepted durably, so losing the observation channel says
      // nothing about whether Cheat Engine executed it.
      throw new RuntimeOutcomeUnknownError(
        `${describeError(cause)} The commands were accepted, so their outcome is unknown until this game's runtime state can be read again.`,
        lastEnvelope,
        generations,
      );
    }
    lastEnvelope = envelope;
    // The batch is already durably accepted, and running it is the very thing
    // that stops Cheat Engine writing its heartbeat. Abandoning the wait on a
    // silent heartbeat reported a disconnected bridge for a session that was
    // executing the user's own command; the poll's own deadline below is what
    // bounds it, and its outcome is "unknown", not "disconnected".
    assertSameSession(before, envelope, appId, true);
    const results = resultsForCommands(envelope, commands);
    if (results) return { envelope, results };
    if (attempt + 1 < ACK_POLL_ATTEMPTS) await delay(ACK_POLL_INTERVAL_MS);
  }
  throw new RuntimeOutcomeUnknownError(
    `Runtime generation ${generations[0]}–${generations[generations.length - 1]} is still pending; wait for bridge reconciliation before retrying.`,
    lastEnvelope,
    generations,
  );
}

export async function sendRuntimeCommandAndWait(
  appId: number,
  spec: RuntimeCommandSpec,
): Promise<{ envelope: RuntimeEnvelope; result: RuntimeResult }> {
  const batch = await sendRuntimeBatchAndWait(appId, [spec]);
  const result = batch.results[0];
  if (!result.ok) {
    throw new RuntimeOperationError(
      result.error ?? "Resident bridge rejected the runtime command.",
      batch.envelope,
    );
  }
  return { envelope: batch.envelope, result };
}

/** Raised when the UI that owns a multi-batch query is disposed while it runs. */
export class RuntimeQueryAbortedError extends Error {
  constructor() {
    super("The runtime query was cancelled before it finished.");
    this.name = "RuntimeQueryAbortedError";
  }
}

async function runForRecordChunks(
  appId: number,
  recordIds: readonly number[],
  build: (recordId: number) => RuntimeCommandSpec,
  validateChunk?: (results: readonly RuntimeResult[], completedBefore: number, envelope: RuntimeEnvelope) => void,
  expectedEnvelope?: RuntimeEnvelope,
  signal?: AbortSignal,
): Promise<{ envelope: RuntimeEnvelope; results: RuntimeResult[] }> {
  if (recordIds.length === 0) {
    let envelope: RuntimeEnvelope | null = null;
    try {
      envelope = await getRuntimeStatus(appId);
      assertConnected(envelope, appId);
      if (expectedEnvelope) assertSameSession(expectedEnvelope, envelope, appId);
    } catch (cause) {
      if (cause instanceof RuntimeOperationError) throw cause;
      throw new RuntimeOperationError(
        describeError(cause),
        envelope,
      );
    }
    return { envelope, results: [] };
  }
  let envelope: RuntimeEnvelope | null = null;
  const allResults: RuntimeResult[] = [];
  for (let offset = 0; offset < recordIds.length; offset += COMMAND_BATCH_SIZE) {
    // Cancellation is checked between batches, never inside one: a generation
    // that has been written has to be waited out or the next action would race
    // its acknowledgement. Closing the picker used to leave the whole remaining
    // query issuing generations against the same control log while the user was
    // already pressing something else on Home.
    if (signal?.aborted) throw new RuntimeQueryAbortedError();
    const chunk = recordIds.slice(offset, offset + COMMAND_BATCH_SIZE);
    let batch: RuntimeBatchResult;
    try {
      batch = await sendRuntimeBatchAndWait(appId, chunk.map(build), expectedEnvelope);
    } catch (cause) {
      if (cause instanceof RuntimeOperationError && !cause.envelope && envelope) {
        throw new RuntimeOperationError(cause.message, envelope);
      }
      throw cause;
    }
    if (envelope) assertSameSession(envelope, batch.envelope, appId);
    validateChunk?.(batch.results, allResults.length, batch.envelope);
    envelope = batch.envelope;
    allResults.push(...batch.results);
  }
  return { envelope: envelope!, results: allResults };
}

export async function queryRuntimeControls(
  appId: number,
  recordIds: readonly number[],
  expectedEnvelope?: RuntimeEnvelope,
): Promise<RuntimeBatchResult> {
  const unique = [...new Set(recordIds)];
  const queried = await runForRecordChunks(
    appId,
    unique,
    (recordId) => ({ kind: "query", record_id: recordId }),
    (results, _completedBefore, envelope) => {
      const failed = results.find((result) => !result.ok);
      if (failed) {
        throw new RuntimeOperationError(
          failed.error ?? `MemoryRecord ${failed.record_id ?? "unknown"} query failed.`,
          envelope,
        );
      }
    },
    expectedEnvelope,
  );
  return { envelope: queried.envelope, results: queried.results };
}

/**
 * Query records without letting one unavailable record invalidate the rest.
 *
 * A record inside a script does not exist until that script has run, which this
 * UI models deliberately. Treating the first unsuccessful record as a failure of
 * the whole call therefore turned an expected per-record state into a
 * session-wide one: Home dropped its entire live snapshot and hid pinned
 * controls, and the picker could not build a model at all, even though the
 * bridge and session were healthy and every other record was readable.
 *
 * Session and descriptor mismatches stay fatal - those really are whole-session
 * failures. Only the bridge's typed `record_missing` result is a per-record
 * state; detach, AddressList and read failures remain fatal.
 */
export async function queryRuntimeControlsPartial(
  appId: number,
  recordIds: readonly number[],
  expectedEnvelope?: RuntimeEnvelope,
  signal?: AbortSignal,
): Promise<RuntimeBatchResult & { unavailable: RuntimeResult[] }> {
  const unique = [...new Set(recordIds)];
  if (unique.length > MAX_LIVE_CONTROLS) {
    throw new RuntimeOperationError(
      `This table has ${unique.length} controls CE Decky can act on; live control is limited to ${MAX_LIVE_CONTROLS} so one refresh cannot take minutes of bridge round-trips.`,
      null,
    );
  }
  const queried = await runForRecordChunks(
    appId,
    unique,
    (recordId) => ({ kind: "query", record_id: recordId }),
    (results, _completedBefore, envelope) => {
      const failed = results.find((result) => !result.ok && result.error_code !== "record_missing");
      if (failed) {
        throw new RuntimeOperationError(
          failed.error ?? `MemoryRecord ${failed.record_id ?? "unknown"} query failed.`,
          envelope,
        );
      }
    },
    expectedEnvelope,
    signal,
  );
  return {
    envelope: queried.envelope,
    results: queried.results.filter((result) => result.ok),
    unavailable: queried.results.filter((result) => !result.ok && result.error_code === "record_missing"),
  };
}

/**
 * Whether this refusal says anything about the table itself.
 *
 * The bridge reports one code for a `set_active` that settled in the wrong
 * state, in either direction, because from its side both are the same fact. A
 * cheat that would not switch *on* is what a table written for a different
 * build of the game does. A cheat that would not switch *off* is the opposite
 * situation - the cheat is very likely still running in the game - and calling
 * the table unusable there would both misdescribe it and hide that.
 */
function refusalIsAboutTheTable(
  failed: RuntimeResult,
  desiredById: ReadonlyMap<number, RuntimeDesiredState>,
): boolean {
  if (failed.error_code !== "activation_rejected") return false;
  const desired = failed.record_id === null ? undefined : desiredById.get(failed.record_id);
  return desired?.active === true;
}

/**
 * The message for one mutation Cheat Engine did not carry out.
 *
 * `activation_rejected` is the refusal a user can actually act on: Cheat Engine
 * ran the record and it came back in the state it started in. A cheat table
 * finds the game's code by scanning for byte patterns, so a table written
 * against an older build of the game fails exactly here - and the bridge's own
 * "activation did not settle" tells nobody that. Every other failure already
 * carries a reason from Cheat Engine or the bridge, and that reason stands.
 */
function describeMutationFailure(
  failed: RuntimeResult,
  desiredById: ReadonlyMap<number, RuntimeDesiredState>,
): string {
  const desired = failed.record_id === null ? undefined : desiredById.get(failed.record_id);
  const subject = desired?.label
    ? `\u201c${desired.label}\u201d`
    : `MemoryRecord ${failed.record_id ?? "unknown"}`;
  if (failed.error_code === "activation_rejected") {
    return desired?.active === false
      ? `${subject} did not switch off: Cheat Engine ran its disable step and the cheat stayed on.`
      : `${subject} did not switch on: Cheat Engine ran it and it went straight back off. A cheat table finds the game's code by scanning for patterns, so this normally means this table was written for a different build of the game.`;
  }
  return failed.error ?? `${subject} mutation failed.`;
}

/**
 * The value this desired state asks for: the switch's key, or what was staged.
 *
 * A switch record's list is its control, so `active` decides what is written
 * and any value staged beside it is ignored. Everything that acts on the value
 * has to agree on this, the write and the verification alike, or a record is
 * written with one and checked against the other.
 */
function wantedValue(desired: RuntimeDesiredState): string | null {
  if (desired.switch_values && desired.active !== null) {
    return desired.active ? desired.switch_values.on : desired.switch_values.off;
  }
  return desired.value;
}

export async function applyRuntimeSelection(
  appId: number,
  desiredStates: readonly RuntimeDesiredState[],
): Promise<RuntimeBatchResult & { compatibilityConfirmed?: boolean; compatibilityMayHaveChanged?: boolean }> {
  const byId = new Map<number, RuntimeDesiredState>();
  for (const state of desiredStates) {
    if (!Number.isInteger(state.record_id) || state.record_id < 0) {
      throw new Error("Runtime desired state contains an invalid MemoryRecord ID.");
    }
    if (byId.has(state.record_id)) {
      throw new Error(`Runtime desired state contains duplicate MemoryRecord ${state.record_id}.`);
    }
    byId.set(state.record_id, state);
  }
  if (byId.size === 0) {
    return queryRuntimeControls(appId, []);
  }

  // Mutation order is directional, because a script owns the records inside it.
  // Enabling a nested cheat needs its enclosing scripts on first, so shallow
  // records go first. Disabling one is the mirror image: switching off a parent
  // can destroy the children it created, so the deepest record must be switched
  // off while it still exists. Sorting parent-first unconditionally - which is
  // what Disable all already avoids - meant Configure cheats could destroy a
  // descendant it was about to verify and report a successful runtime change as
  // a failure, leaving the remembered state unsaved.
  const byDepth = (left: { state: RuntimeDesiredState; index: number }, right: { state: RuntimeDesiredState; index: number }, deepestFirst: boolean) => {
    const leftPath = left.state.path;
    const rightPath = right.state.path;
    if (leftPath && rightPath) {
      const depth = leftPath.length - rightPath.length;
      if (depth) return deepestFirst ? -depth : depth;
    }
    // Preserve caller/inspection order when no complete path ordering is available.
    return left.index - right.index;
  };
  const indexed = [...byId.values()].map((state, index) => ({ state, index }));
  const enabling = indexed
    .filter(({ state }) => state.active !== false)
    .sort((left, right) => byDepth(left, right, false))
    .map(({ state }) => state);
  const disabling = indexed
    .filter(({ state }) => state.active === false)
    .sort((left, right) => byDepth(left, right, true))
    .map(({ state }) => state);
  // Retire descendants before the ancestors that own them, then build upward.
  const ordered = [...disabling, ...enabling];
  const ids = ordered.map((state) => state.record_id);
  const isAncestorOf = (ancestor: RuntimeDesiredState, descendant: RuntimeDesiredState): boolean =>
    Boolean(ancestor.path && descendant.path)
    && ancestor.path!.length < descendant.path!.length
    && ancestor.path!.every((segment, position) => descendant.path![position] === segment);
  // A record that does not exist yet is expected exactly when an ancestor of it
  // is being enabled in this same call - that ancestor is the script that
  // creates it. Preflighting every ID strictly aborted before the parent
  // activation was ever sent, which is the one case the ordering above exists
  // for: enabling a nested cheat could never work on a dynamic table.
  const createdByAnAncestorHere = (state: RuntimeDesiredState): boolean =>
    ordered.some((other) => other !== state && other.active === true && isAncestorOf(other, state));

  const before = await queryRuntimeControlsPartial(appId, ids);
  const current = new Map(before.results.map((result) => [result.record_id, result]));
  const missing = new Set(before.unavailable.flatMap((result) => result.record_id === null ? [] : [result.record_id]));
  const alreadyAbsentOff = new Set<number>();
  const deferred: RuntimeDesiredState[] = [];
  for (const desired of ordered) {
    if (!missing.has(desired.record_id)) continue;
    // Already unreachable and wanted off: its enclosing script is not running,
    // so the record is inactive by construction and there is nothing to send.
    if (desired.active === false) {
      alreadyAbsentOff.add(desired.record_id);
      continue;
    }
    if (createdByAnAncestorHere(desired)) {
      deferred.push(desired);
      continue;
    }
    throw new RuntimeOperationError(
      `${desired.label ?? `MemoryRecord ${desired.record_id}`} does not exist in the running table.`,
      before.envelope,
    );
  }

  const activatedHere = new Set<number>();
  const commandsFor = (desired: RuntimeDesiredState, observed: RuntimeResult): RuntimeCommandSpec[] => {
    const specs: RuntimeCommandSpec[] = [];
    const wanted = wantedValue(desired);
    const setValue: RuntimeCommandSpec[] = wanted !== null && observed.value !== wanted
      ? [{ kind: "set_value", record_id: desired.record_id, value: wanted }]
      : [];
    const setActive: RuntimeCommandSpec[] = desired.active !== null && observed.active !== desired.active
      ? [{ kind: "set_active", record_id: desired.record_id, value: desired.active ? "1" : "0" }]
      : [];
    // Only a record this call actually switched on counts as proof it did:
    // one already on was not made to work here.
    if (setActive.length > 0 && desired.active) activatedHere.add(desired.record_id);
    // Switching a record off releases the freeze, and a value written after
    // that is what the game keeps; written before it, Cheat Engine is still
    // holding the record and the write is what the freeze is then released on.
    // The end state is the same either way, and this order is the one where a
    // failed release leaves nothing written.
    specs.push(...(desired.active === false ? [...setActive, ...setValue] : [...setValue, ...setActive]));
    return specs;
  };

  const mutations: RuntimeCommandSpec[] = [];
  for (const desired of ordered) {
    const observed = current.get(desired.record_id);
    if (!observed) continue;
    mutations.push(...commandsFor(desired, observed));
  }

  let lastEnvelope = before.envelope;
  for (let offset = 0; offset < mutations.length; offset += COMMAND_BATCH_SIZE) {
    const batch = await sendRuntimeBatchAndWait(
      appId,
      mutations.slice(offset, offset + COMMAND_BATCH_SIZE),
      before.envelope,
    );
    assertSameSession(before.envelope, batch.envelope, appId);
    const failed = batch.results.find((result) => !result.ok);
    if (failed) {
      throw new RuntimeOperationError(
        describeMutationFailure(failed, byId), batch.envelope, refusalIsAboutTheTable(failed, byId),
      );
    }
    lastEnvelope = batch.envelope;
  }

  // The ancestors are on now, so the records they create can be asked for. Wait
  // boundedly for Cheat Engine to build them rather than assuming one round
  // trip is enough, then mutate them shallowest-first like any other enable.
  if (deferred.length > 0) {
    const stillMissing = new Map(deferred.map((state) => [state.record_id, state]));
    for (let attempt = 0; attempt < MATERIALIZATION_ATTEMPTS && stillMissing.size > 0; attempt += 1) {
      if (attempt > 0) await new Promise((resolve) => setTimeout(resolve, MATERIALIZATION_DELAY_MS));
      const observed = await queryRuntimeControlsPartial(appId, [...stillMissing.keys()], before.envelope);
      assertSameSession(before.envelope, observed.envelope, appId);
      lastEnvelope = observed.envelope;
      const pending: RuntimeCommandSpec[] = [];
      for (const result of observed.results) {
        if (result.record_id === null) continue;
        const desired = stillMissing.get(result.record_id);
        if (!desired) continue;
        pending.push(...commandsFor(desired, result));
        stillMissing.delete(result.record_id);
      }
      for (let offset = 0; offset < pending.length; offset += COMMAND_BATCH_SIZE) {
        const batch = await sendRuntimeBatchAndWait(
          appId, pending.slice(offset, offset + COMMAND_BATCH_SIZE), before.envelope,
        );
        assertSameSession(before.envelope, batch.envelope, appId);
        const failed = batch.results.find((result) => !result.ok);
        if (failed) {
          throw new RuntimeOperationError(
        describeMutationFailure(failed, byId), batch.envelope, refusalIsAboutTheTable(failed, byId),
      );
        }
        lastEnvelope = batch.envelope;
      }
    }
    const unresolved = [...stillMissing.values()][0];
    if (unresolved) {
      throw new RuntimeOperationError(
        `${unresolved.label ?? `MemoryRecord ${unresolved.record_id}`} did not appear after its enclosing scripts were switched on.`,
        lastEnvelope,
      );
    }
  }

  // Verification must not require a record this very call deliberately
  // destroyed. Switching a script off removes the records it created, so a
  // descendant of a record we just switched off is expected to be unreadable
  // and its intended state is already known: off.
  const deliberatelyOff = new Set(disabling.map((state) => state.record_id));
  const destroyedByAncestor = (state: RuntimeDesiredState): boolean => {
    if (!state.path || state.active !== false) return false;
    return ordered.some((other) =>
      other !== state
      && other.active === false
      && Boolean(other.path)
      && other.path!.length < state.path!.length
      && other.path!.every((segment, position) => state.path![position] === segment),
    );
  };
  const verified = await queryRuntimeControlsPartial(appId, ids, before.envelope);
  assertSameSession(before.envelope, verified.envelope, appId);
  for (const missing of verified.unavailable) {
    if (missing.record_id === null) continue;
    const desired = byId.get(missing.record_id);
    if (
      desired
      && deliberatelyOff.has(missing.record_id)
      && (alreadyAbsentOff.has(missing.record_id) || destroyedByAncestor(desired))
    ) continue;
    throw new RuntimeOperationError(
      missing.error ?? `MemoryRecord ${missing.record_id} query failed.`,
      verified.envelope,
    );
  }
  for (const result of verified.results) {
    if (result.record_id === null) continue;
    const desired = byId.get(result.record_id);
    if (!desired) continue;
    if (desired.active !== null && result.active !== desired.active) {
      throw new RuntimeOperationError(
        `${desired.label ?? `MemoryRecord ${result.record_id}`} did not switch ${desired.active ? "on" : "off"}. Cheat Engine reported ${describeReadBack(result.active === null ? null : String(result.active))}.`,
        verified.envelope,
      );
    }
    // Against what was actually asked for, which for a switch record is the key
    // its toggle writes rather than whatever value was staged beside it.
    const wanted = wantedValue(desired);
    if (wanted !== null && result.value !== wanted) {
      throw new RuntimeOperationError(
        `${desired.label ?? `MemoryRecord ${result.record_id}`} kept ${describeReadBack(result.value)} instead of ${wanted}.`,
        verified.envelope,
      );
    }
  }
  const proofTarget = ordered.find((candidate) => activatedHere.has(candidate.record_id) && candidate.active === true && !ordered.some((other) =>
    other !== candidate && other.active === true && candidate.path && other.path
    && candidate.path.length < other.path.length
    && candidate.path.every((segment, index) => other.path![index] === segment),
  ));
  const prepared = verified.envelope?.prepared;
  let compatibilityConfirmed = false;
  // A confirmation that throws has not necessarily written nothing: the record
  // can be committed and visible and still report a durability the backend
  // cannot prove. So the two answers are kept apart - what is known to have
  // been stored, and what may have been - and a caller that shows compatibility
  // history rereads it for either, because the alternative is a panel that
  // disagrees with the backend until it is remounted.
  let compatibilityMayHaveChanged = false;
  if (proofTarget && prepared) {
    compatibilityMayHaveChanged = true;
    try {
      compatibilityConfirmed = await confirmTableWorking(appId, prepared.table_sha256, prepared.session_id, proofTarget.record_id);
    } catch (error) {
      logUiWarning("runtime.compatibility_not_recorded", { app_id: appId, reason: describeError(error) });
    }
  }
  return { envelope: verified.envelope ?? lastEnvelope, results: verified.results, compatibilityConfirmed, compatibilityMayHaveChanged };
}

/**
 * Switch off everything that is on, and leave every switch at its off key.
 *
 * `offValues` is that key per record. Releasing the freeze is not switching
 * such a cheat off: the record keeps the value it was frozen at, so a table of
 * `0:Disabled/1:Enabled` flags would report nothing active while every flag in
 * the game stayed at 1. A comment inside the parameter list would land in the
 * committed bundle as trailing whitespace, which is why this is here.
 */
export async function deactivateAllActiveControls(
  appId: number,
  recordIds: readonly number[],
  offValues: ReadonlyMap<number, string> = new Map(),
): Promise<DeactivateAllResult> {
  const unique = [...new Set(recordIds)];
  if (unique.length > MAX_LIVE_CONTROLS) {
    throw new RuntimeOperationError(
      `This table has ${unique.length} controls CE Decky can act on; live control is limited to ${MAX_LIVE_CONTROLS}. Switch cheats off from the picker instead.`,
      null,
    );
  }
  if (unique.length === 0) {
    const empty = await queryRuntimeControls(appId, []);
    return { queried: 0, active: 0, deactivated: 0, deactivatedIds: [], envelope: empty.envelope };
  }

  let queried: Awaited<ReturnType<typeof runForRecordChunks>>;
  let queriedBeforeFailure = 0;
  try {
    queried = await runForRecordChunks(
      appId,
      unique,
      (recordId) => ({ kind: "query", record_id: recordId }),
      (results, completedBefore, envelope) => {
        // A record whose enclosing script is not running does not exist, and it
        // is inactive by construction - which is exactly the state this call
        // wants. Requiring every ID to answer meant one unmaterialized child
        // blocked Disable all before a single command was sent. A record that
        // answers but cannot report an active state is still a real failure.
        const failures = results.filter((result) =>
          (!result.ok && result.error_code !== "record_missing")
          || (result.ok && result.active === null)
        );
        queriedBeforeFailure = completedBefore + (results.length - failures.length);
        if (failures.length === 0) return;
        const first = failures[0];
        throw new RuntimeOperationError(
          `Bulk deactivation stopped before mutation: ${failures.length} state quer${failures.length === 1 ? "y" : "ies"} failed in the current batch; ${first.error ?? `MemoryRecord ${first.record_id ?? "unknown"} did not return an active state`}.`,
          envelope,
        );
      },
    );
  } catch (cause) {
    if (cause instanceof RuntimeOperationError) {
      const prefix = queriedBeforeFailure > 0 && !cause.message.startsWith("Bulk deactivation stopped before mutation:")
        ? `Bulk deactivation stopped before mutation after querying ${queriedBeforeFailure}/${unique.length} control(s); `
        : "";
      throw new RuntimeOperationError(`${prefix}${cause.message}`, cause.envelope);
    }
    throw cause;
  }
  const activeIds = queried.results
    .filter((result) => result.record_id !== null && result.active === true)
    .map((result) => result.record_id as number);
  if (activeIds.length === 0) {
    return { queried: unique.length, active: 0, deactivated: 0, deactivatedIds: [], envelope: queried.envelope };
  }

  let deactivatedBeforeFailure = 0;
  try {
    await runForRecordChunks(
      appId,
      activeIds,
      (recordId) => ({ kind: "set_active", record_id: recordId, value: "0" }),
      (results, completedBefore, envelope) => {
        const failures = results.filter((result) => !result.ok || result.active !== false);
        const successful = results.length - failures.length;
        deactivatedBeforeFailure = completedBefore + successful;
        if (failures.length === 0) return;
        const first = failures[0];
        throw new RuntimeOperationError(
          `Bulk deactivation incomplete: ${deactivatedBeforeFailure}/${activeIds.length} command(s) acknowledged inactive; ${first.error ?? `MemoryRecord ${first.record_id ?? "unknown"} did not acknowledge inactive`}. No later batch was sent; refresh/query state before retrying.`,
          envelope,
        );
      },
      queried.envelope,
    );
  } catch (cause) {
    if (cause instanceof RuntimeOperationError) {
      const prefix = deactivatedBeforeFailure > 0 && !cause.message.startsWith("Bulk deactivation incomplete:")
        ? `Bulk deactivation interrupted after ${deactivatedBeforeFailure}/${activeIds.length} command(s) acknowledged inactive; `
        : "";
      throw new RuntimeOperationError(`${prefix}${cause.message}`, cause.envelope);
    }
    throw cause;
  }

  let verified: Awaited<ReturnType<typeof runForRecordChunks>>;
  let verifiedBeforeFailure = 0;
  try {
    verified = await runForRecordChunks(
      appId,
      activeIds,
      (recordId) => ({ kind: "query", record_id: recordId }),
      (results, completedBefore, envelope) => {
        // The mutation phase deliberately switches the deepest record off first,
        // because an enclosing script destroys the records it created. Requiring
        // every one of those records to still answer therefore made the intended
        // successful end state indistinguishable from an error: child off, parent
        // off, parent destroys child, child query returns "MemoryRecord missing",
        // and Disable all threw - so the durable profile kept remembering cheats
        // the user had just switched off. A record that has ceased to exist after
        // its own deactivation was acknowledged is the desired outcome.
        const failures = results.filter((result) =>
          (!result.ok && result.error_code !== "record_missing")
          || (result.ok && result.active !== false)
        );
        const successful = results.length - failures.length;
        verifiedBeforeFailure = completedBefore + successful;
        if (failures.length === 0) return;
        const first = failures[0];
        throw new RuntimeOperationError(
          `Bulk deactivation verification incomplete: ${verifiedBeforeFailure}/${activeIds.length} record(s) re-queried inactive; ${first.error ?? `MemoryRecord ${first.record_id ?? "unknown"} did not verify inactive`}. No later verification batch was sent.`,
          envelope,
        );
      },
      queried.envelope,
    );
  } catch (cause) {
    if (cause instanceof RuntimeOperationError) {
      const prefix = verifiedBeforeFailure > 0 && !cause.message.startsWith("Bulk deactivation verification incomplete:")
        ? `Bulk deactivation verification interrupted after ${verifiedBeforeFailure}/${activeIds.length} record(s) re-queried inactive; `
        : "";
      throw new RuntimeOperationError(`${prefix}${cause.message}`, cause.envelope);
    }
    throw cause;
  }
  // Every record that was switched off and declares an off key is put back to
  // it, after the release rather than before it, so the value the game keeps is
  // the one written last. A record that ceased to exist with its own script is
  // skipped: it has no address to write to and its absence is the desired end.
  let envelope = verified.envelope;
  // The freshest answer per record, not the first: a record can be read several
  // times in one verification and only the last one says whether it is still
  // there to be written to.
  const latest = new Map<number, RuntimeResult>();
  for (const result of verified.results) {
    if (result.record_id !== null) latest.set(result.record_id, result);
  }
  const restored = new Map<number, string>();
  for (const recordId of activeIds) {
    const value = offValues.get(recordId);
    if (value !== undefined && latest.get(recordId)?.ok) restored.set(recordId, value);
  }
  if (restored.size > 0) {
    const written = await runForRecordChunks(
      appId,
      [...restored.keys()],
      (recordId) => ({ kind: "set_value", record_id: recordId, value: restored.get(recordId)! }),
      (results, _completedBefore, batchEnvelope) => {
        const failed = results.find((result) => !result.ok);
        if (!failed) return;
        throw new RuntimeOperationError(
          leadWithCause(
            failed.error ?? `MemoryRecord ${failed.record_id ?? "unknown"} refused the write`,
            "Every cheat was switched off, but one could not be put back to its off value, so that cheat may still be running in the game.",
          ),
          batchEnvelope,
        );
      },
      verified.envelope,
    );
    envelope = written.envelope;
  }
  return {
    queried: unique.length,
    active: activeIds.length,
    deactivated: activeIds.length,
    deactivatedIds: activeIds,
    envelope,
  };
}

/** How Cheat Engine answered a read-back, in words a user can act on. */
function describeReadBack(value: string | null): string {
  const text = (value ?? "").trim();
  if (!text || text === "??") {
    return "no readable value, which usually means the script that creates this address is not enabled yet";
  }
  return text;
}
