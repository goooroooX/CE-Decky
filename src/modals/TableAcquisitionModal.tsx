import { useUiSurface } from "../useUiSurface";
import { traceUiAction, traceUiEdit, startUiOperation } from "../uiActions";
import { DialogButton, DropdownItem, Field, Focusable, ModalRoot, PanelSection, PanelSectionRow, Spinner, TextField } from "@decky/ui";
import { toaster } from "@decky/api";
import { useEffect, useMemo, useRef, useState } from "react";
import { cancelTableAcquisition, completeTableAcquisition, pollTableAcquisition } from "../api";
import { ModalActions, modalActionStyle } from "../components/ModalActions";
import { UNSUPPORTED_MEMBER_EXPLANATION, canAutoImportTableMember, isUnsupportedEncryptedMember, passwordPromptRequired, rememberRejectedArtifact } from "../tableImport";
import type { AcquisitionStatus } from "../types";
import { providerShortName } from "../uiModel";
import { describeError, pythonTracebackSummary } from "../errors";
import { DensePanel, PanelRow, SectionHeading } from "../components/PanelDensity";
import { logUi, logUiFailure, logUiWarning } from "../supportLog";

/**
 * How many refused status reads in a row are allowed before this window gives
 * up on the acquisition behind them.
 *
 * A read that fails says nothing about the transfer: the backend can be moving
 * bytes perfectly well behind one refused RPC. So the first few are retried,
 * widening, and only a run of them is treated as the acquisition being beyond
 * reach. The budget is the length of the delay list plus the attempt that ends
 * it.
 */
const POLL_FAILURE_BUDGET = 4;
const POLL_RETRY_DELAYS_MS = [500, 1500, 3000];

/**
 * Whether the backend has said, authoritatively, that it no longer has this
 * acquisition.
 *
 * That answer is terminal the moment it arrives, and it is also the one answer
 * that must never make this window try again: there is nothing to poll, nothing
 * to cancel, and a Close that cancels first would re-raise it and shut the user
 * in. Every other failure is a failure to observe, not an outcome.
 *
 * Matched on the backend's own sentence, which is the only thing a Decky RPC
 * rejection carries across. `acquisition.py` raises it in exactly two places
 * and `tests/test_acquisition_prod.py` holds the wording, so a change to it
 * fails there rather than quietly turning this back into a trap.
 */
function describesForgottenAcquisition(cause: unknown): boolean {
  return /acquisition is unknown or expired/i.test(describeError(cause));
}

interface Props {
  initialStatus: AcquisitionStatus;
  /** The exact cached search snapshot these bytes were offered from. */
  searchScope: string;
  onImported: (sha256: string) => Promise<void>;
  onClose: () => void;
}

function active(status: AcquisitionStatus): boolean {
  return status.state === "waiting_provider" || status.state === "downloading";
}

function terminal(status: AcquisitionStatus): boolean {
  return status.state === "imported" || status.state === "failed" || status.state === "cancelled";
}

export function TableAcquisitionModal({ initialStatus, searchScope, onImported, onClose }: Props) {
  useUiSurface("TableAcquisitionModal", initialStatus.acquisition_id);
  const [status, setStatus] = useState(initialStatus);
  const [memberPath, setMemberPath] = useState<string | null>(null);
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const statusRef = useRef(initialStatus);
  const busyRef = useRef(false);
  const completingRef = useRef(false);
  // Whether the one password-less attempt that lets the backend try the
  // provider's published password has already been spent for this acquisition.
  const hintTriedRef = useRef(false);
  const members = useMemo(() => status.inspection?.members ?? [], [status.inspection]);
  const selectedMember = members.find((member) => member.path === memberPath) ?? members[0] ?? null;
  const needsTypedPassword = passwordPromptRequired(status, hintTriedRef.current);
  const unsupportedMember = isUnsupportedEncryptedMember(selectedMember);
  // The manual row appears exactly when something is still left to decide: a
  // choice of member, or a password that the backend's own attempt could not
  // supply. Before that attempt a single encrypted member is auto-imported.
  // One password-less attempt is allowed first so the backend can try the
  // password the provider published beside this exact artifact; only after
  // that fails does the user have to know it themselves.
  const manualImportNeeded = members.length > 0 && !canAutoImportTableMember(members, needsTypedPassword);
  /** Whether the one press this window can still be asked for is available. */
  const importable = !busy
    && (status.state === "ready_to_import" || status.state === "needs_selection")
    && manualImportNeeded
    && Boolean(selectedMember)
    && !unsupportedMember
    && !(selectedMember?.encrypted && !password && needsTypedPassword);

  // Set once the backend has said it no longer has this acquisition. Nothing
  // can be cancelled or polled after that, and Close must not depend on it.
  const forgottenRef = useRef(false);
  /**
   * Why this window cannot currently read the acquisition's state, and nothing
   * while it can.
   *
   * Kept apart from the acquisition's own state on purpose. A run of refused
   * status reads says nothing whatever about the transfer behind them, and
   * writing `failed` into the status was not a label but an action: a terminal
   * status turns the only press here into Close, and Close cancels what it
   * closes, so a download that was proceeding normally was cancelled because
   * this window had stopped being able to look at it.
   */
  const [readError, setReadError] = useState<string | null>(null);
  /** Bumped by Retry, to start the loop the exhausted budget stopped. */
  const [readAttempt, setReadAttempt] = useState(0);

  const remember = (next: AcquisitionStatus) => {
    statusRef.current = next;
    rememberRejectedArtifact(searchScope, next);
    setStatus(next);
    const nextMembers = next.inspection?.members ?? [];
    setMemberPath((current) => current && nextMembers.some((member) => member.path === current)
      ? current
      : nextMembers[0]?.path ?? null);
  };

  // An acquisition that already retired the artifact before this modal opened
  // never reaches the poll loop, so record its outcome on mount as well.
  useEffect(() => {
    rememberRejectedArtifact(searchScope, initialStatus);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!active(status)) return;
    let disposed = false;
    let timer: number | undefined;
    let consecutiveFailures = 0;
    // eslint-disable-next-line @typescript-eslint/no-unused-expressions
    readAttempt;
    const poll = async () => {
      try {
        const next = await pollTableAcquisition(status.acquisition_id);
        if (disposed) return;
        consecutiveFailures = 0;
        setReadError(null);
        remember(next);
        if (active(next)) timer = window.setTimeout(() => void poll(), 750);
      } catch (cause) {
        if (disposed) return;
        // A failed read of the state is not a failed download. The backend may
        // be transferring perfectly well behind one refused RPC, and writing
        // `failed` here does not only mislabel it: unmounting this window
        // cancels anything not terminal, so an observation that failed for a
        // moment became the abort of a healthy transfer. Every provider
        // download goes through this window, so that was the whole of Search.
        //
        // Two answers are not the same. The backend saying it does not have
        // this acquisition is authoritative and terminal at once. Anything else
        // is retried, and only a run of them is allowed to give up.
        const forgotten = describesForgottenAcquisition(cause);
        if (forgotten) {
          // The one authoritative answer: there is no such acquisition, so it
          // is terminal on arrival and there is nothing left to poll or cancel.
          logUiFailure("acquisition.poll_failed", cause, {
            acquisition: status.acquisition_id.slice(0, 8), forgotten: true,
          });
          forgottenRef.current = true;
          remember({ ...statusRef.current, state: "failed", error: describeError(cause) });
          return;
        }
        consecutiveFailures += 1;
        if (consecutiveFailures < POLL_FAILURE_BUDGET) {
          logUiWarning("acquisition.poll_retrying", {
            acquisition: status.acquisition_id.slice(0, 8),
            attempt: consecutiveFailures,
            reason: describeError(cause),
          });
          timer = window.setTimeout(() => void poll(), POLL_RETRY_DELAYS_MS[consecutiveFailures - 1] ?? 2000);
          return;
        }
        // The budget is exhausted, and that is a statement about this window's
        // ability to look, never about the acquisition. The last state the
        // backend actually reported stays exactly as it was: the download is
        // very likely still running, and the user is told that rather than
        // being handed a Close that would cancel it.
        logUiFailure("acquisition.poll_unreadable", cause, {
          acquisition: status.acquisition_id.slice(0, 8),
          attempts: consecutiveFailures,
        });
        setReadError(describeError(cause));
      }
    };
    timer = window.setTimeout(() => void poll(), 250);
    return () => {
      disposed = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [status.acquisition_id, active(status), readAttempt]);

  useEffect(() => () => {
    const current = statusRef.current;
    // Nothing to cancel where the backend has already said it has nothing.
    if (forgottenRef.current) return;
    if (current.state !== "imported" && current.state !== "cancelled") {
      // Best effort, and the failure matters: this modal is gone, so a
      // cancellation that did not land leaves acquisition work running with
      // nothing on screen that could report or stop it.
      void cancelTableAcquisition(current.acquisition_id).catch((cause) => {
        logUiWarning("acquisition.unmount_cancel_failed", {
          acquisition: current.acquisition_id.slice(0, 8),
          reason: describeError(cause),
        });
      });
    }
  }, []);

  const complete = async (chosenMember: string | null = memberPath) => {
    if (busyRef.current || completingRef.current) return;
    let handoffStarted = false;
    const operation = startUiOperation("acquisition.complete", { acquisition: statusRef.current.acquisition_id });
    completingRef.current = true;
    busyRef.current = true;
    setBusy(true);
    try {
      let next;
      try {
        next = await completeTableAcquisition(
          statusRef.current.acquisition_id,
          null,
          chosenMember,
          password || null,
        );
      } catch (cause) {
        // Import can already have reached its terminal state when the reply is
        // lost, and painting that as failed both redoes the work and leaves an
        // imported table undiscovered by this flow. The acquisition ID is the
        // exact authority, so ask it before believing the rejection.
        if (pythonTracebackSummary((cause as { pythonTraceback?: unknown })?.pythonTraceback) !== null) throw cause;
        next = await pollTableAcquisition(statusRef.current.acquisition_id);
        if (!next.imported) throw cause;
        logUiWarning("acquisition.receipt_reconciled", { acquisition: next.acquisition_id, table_sha: next.imported.sha256 });
      }
      remember(next);
      if (next.imported) {
        toaster.toast({ title: "CE Decky", body: `Imported ${next.imported.filename}. Execution remains disabled.` });
        // Close this top modal before the parent Search modal closes and opens
        // Review. Otherwise controller focus depends on asynchronous unmount
        // timing instead of the intended serial modal handoff.
        handoffStarted = true;
        onClose();
        await onImported(next.imported.sha256);
      }
      operation.completed({ state: next.state, table_sha: next.imported?.sha256 });
    } catch (cause) {
      operation.failed(cause, { handoff_started: handoffStarted });
      // Logged either way. After the handoff this modal is gone and Search owns
      // the error on screen, but the record of what failed belongs in the ring
      // regardless of which surface ends up showing it.
      logUiFailure("acquisition.complete_failed", cause, {
        acquisition: statusRef.current.acquisition_id.slice(0, 8),
        handoff_started: handoffStarted,
      });
      // Search owns errors after the handoff begins. This modal is already
      // closed and must not repaint an imported acquisition as failed because
      // later Review preparation failed.
      if (!handoffStarted) {
        remember({ ...statusRef.current, state: "failed", error: describeError(cause) });
      }
    } finally {
      completingRef.current = false;
      busyRef.current = false;
      setBusy(false);
    }
  };

  useEffect(() => {
    // `needs_selection` is also where a single encrypted member arrives: the
    // backend classifies any encrypted member that way, whether or not there is
    // anything to select. Running only for `ready_to_import` meant that archive
    // showed no password field - nothing was left to decide until the hint had
    // been tried - while nothing ever tried it, so the download dead-ended.
    if (
      (status.state !== "ready_to_import" && status.state !== "needs_selection")
      || !canAutoImportTableMember(members, needsTypedPassword)
      || completingRef.current
    ) return;
    if (members.length === 1 && members[0].encrypted) hintTriedRef.current = true;
    void complete(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status.acquisition_id, status.state]);

  // Where focus belongs once the download stops being the thing on screen.
  //
  // Steam draws the focus ring on the element it last moved to and repaints it
  // when input moves. A row that was focused while the download ran can change
  // underneath it - the countdown and the spinner go, a failure appears below -
  // and the ring stays where it was until the user pushes the stick, which
  // reads as the dialog having broken rather than finished. Moving focus onto
  // the action that now matters is the same thing the user was about to do, and
  // it repaints the ring as a side effect of being an honest handoff.
  const actionsRef = useRef<HTMLDivElement | null>(null);
  const settledRef = useRef(terminal(initialStatus));
  useEffect(() => {
    if (!terminal(status)) {
      settledRef.current = false;
      return;
    }
    if (settledRef.current) return;
    settledRef.current = true;
    const button = actionsRef.current?.querySelector<HTMLElement>("button:not([disabled])");
    button?.focus();
  }, [status.state]);

  const cancel = async () => {
    if (busyRef.current) return;
    if (statusRef.current.state === "imported" || statusRef.current.state === "cancelled") {
      onClose();
      return;
    }
    // An acquisition the backend has forgotten cannot be cancelled, and asking
    // it to fails the same way the read did. Close has to be a way out of this
    // window rather than a second attempt at the object that is not there:
    // failing here left the only two presses this window has, Close and the
    // controller's Back, both re-raising the same error, with the user shut in.
    if (forgottenRef.current) {
      onClose();
      return;
    }
    busyRef.current = true;
    setBusy(true);
    const cancellation = startUiOperation("acquisition.cancel", { acquisition: statusRef.current.acquisition_id });
    try {
      remember(await cancelTableAcquisition(statusRef.current.acquisition_id));
      logUi("acquisition.cancelled", { acquisition: statusRef.current.acquisition_id });
      onClose();
      cancellation.completed({ state: statusRef.current.state });
    } catch (cause) {
      const forgotten = describesForgottenAcquisition(cause);
      cancellation.failed(cause, { forgotten });
      logUiFailure("acquisition.cancel_failed", cause, {
        acquisition: statusRef.current.acquisition_id.slice(0, 8),
        forgotten,
      });
      if (forgotten) {
        // The same answer, arriving on this press instead of on a poll: there
        // is nothing left to cancel, so the press does what it says.
        forgottenRef.current = true;
        onClose();
        return;
      }
      remember({ ...statusRef.current, state: "failed", error: describeError(cause) });
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  // A provider countdown and a provider rate limit are both a wait with a
  // number on it and nothing else about them is the same: one was promised and
  // ends in a download, the other is the provider refusing right now and being
  // given time. Naming Playground for both was wrong twice over, because the
  // rate limit was first seen on a different provider entirely.
  //
  // The number is kept apart from the sentence and put on the right of the same
  // block as the file it belongs to: it is the only part of this row that
  // changes every second, and reading a countdown means finding it again at the
  // end of a sentence that never changes.
  const waiting = status.state === "waiting_provider" && status.provider_wait_seconds !== null;
  const phase = waiting
    ? status.provider_wait_reason === "rate_limited"
      // Named as the wait it is. "Rate limiting" says what the source is
      // doing; what a user staring at a countdown needs is that nothing has
      // failed and the download goes ahead by itself when it reaches zero.
      ? "Waiting out the source's download cooldown"
      : status.provider_wait_reason === "busy"
        ? `${providerShortName(status.provider)} serves one download at a time and is busy`
        : `${providerShortName(status.provider)} is preparing the download`
    : status.state === "downloading"
      ? "Downloading"
      : status.state.replace(/_/g, " ");
  const counter = waiting
    ? status.provider_wait_reason === "rate_limited" || status.provider_wait_reason === "busy"
      ? `retrying in ${status.provider_wait_seconds}s`
      : `${status.provider_wait_seconds}s remaining`
    : status.state === "downloading"
      ? `${status.bytes_received} byte(s)`
      : null;

  return (
    <ModalRoot onCancel={traceUiAction("table_acquisition_modal.cancel_back", () => void cancel(), { acquisition: status.acquisition_id })}>
      <Focusable style={{ minWidth: 420, maxWidth: 600 }}>
        <DensePanel>
          <PanelSection>
            <SectionHeading>{`Download table from ${providerShortName(status.provider)}`}</SectionHeading>
          {/* The spinner sits under the countdown, in the block that names the
              file both belong to. It takes no row of its own, so nothing below
              it moves down for the length of the download and back up after,
              and the two things that say this is still running say it in one
              place instead of at opposite ends of the dialog. */}
          <PanelSectionRow><PanelRow
            label={status.filename}
            description={phase}
            trailing={counter || active(status) ? (
              <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 4 }}>
                {counter}
                {active(status) ? <Spinner style={{ width: 14, height: 14 }} /> : null}
              </div>
            ) : undefined}
          /></PanelSectionRow>
          {status.error && <PanelSectionRow><Field label={status.state === "failed" ? "Acquisition failed" : "Import needs attention"} description={status.error} /></PanelSectionRow>}
          {/* A statement about this window, kept apart from the download's own
              state and worded as what it is. The download is very likely still
              running, and the press below still says Cancel acquisition rather
              than Close, because cancelling is exactly what it would do. */}
          {readError && <PanelSectionRow><PanelRow
            testId="acquisition-status-unreadable"
            status
            label="Cannot read this download's progress"
            description={`${readError} The download itself has not been stopped and may still be running. Retry status looks again; Cancel acquisition stops it.`}
          /></PanelSectionRow>}
          {(status.state === "ready_to_import" || status.state === "needs_selection") && manualImportNeeded && <>
            {members.length > 1 && <PanelSectionRow><DropdownItem
              label="Table in downloaded archive"
              rgOptions={members.map((member) => ({ data: member.path, label: member.path }))}
              selectedOption={selectedMember?.path ?? ""}
              onChange={traceUiAction("table_acquisition_modal.table_in_downloaded_archive", (option) => { setMemberPath(String(option.data)); setPassword(""); }, { acquisition: status.acquisition_id })}
              disabled={busy}
            /></PanelSectionRow>}
            {/* Saying why beats a disabled button with no explanation: nothing
                the user can type opens an encrypted 7z. */}
            {unsupportedMember && (
              <PanelSectionRow>
                <Field label="This entry cannot be imported" description={UNSUPPORTED_MEMBER_EXPLANATION} />
              </PanelSectionRow>
            )}
            {selectedMember?.encrypted && !unsupportedMember && (
              <PanelSectionRow>
                <TextField
                  label={needsTypedPassword ? "Archive password (not stored)" : "Archive password (optional; not stored)"}
                  value={password}
                  onChange={traceUiEdit("table_acquisition_modal.password", (event: any) => setPassword(String(event.target.value ?? "")), { acquisition: status.acquisition_id })}
                  disabled={busy}
                />
              </PanelSectionRow>
            )}
          </>}
          </PanelSection>
        </DensePanel>
        <ModalActions containerRef={actionsRef}>
          {/* The ring opens on the import while there is one to make, and on
              the way out otherwise, which is also what a settled download wants:
              nothing is left to decide and Close is the only press there is. */}
          {(status.state === "ready_to_import" || status.state === "needs_selection") && manualImportNeeded && <DialogButton
            style={modalActionStyle}
            preferredFocus={importable}
            disabled={!importable}
            onClick={traceUiAction("table_acquisition_modal.import_selected_table", () => void complete(selectedMember?.path ?? null), { acquisition: status.acquisition_id })}
          >Import selected table</DialogButton>}
          {readError && <DialogButton
            style={modalActionStyle}
            disabled={busy}
            onClick={traceUiAction("table_acquisition_modal.retry_status", () => { setReadError(null); setReadAttempt((attempt) => attempt + 1); }, { acquisition: status.acquisition_id })}
          >Retry status</DialogButton>}
          <DialogButton style={modalActionStyle} preferredFocus={!importable} disabled={busy} onClick={traceUiAction("table_acquisition_modal.cancel", () => void cancel(), { acquisition: status.acquisition_id })}>
            {terminal(status) ? "Close" : "Cancel acquisition"}
          </DialogButton>
        </ModalActions>
      </Focusable>
    </ModalRoot>
  );
}
