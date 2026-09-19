import { useUiSurface } from "../useUiSurface";
import { traceUiAction, startUiOperation } from "../uiActions";
import { DialogButton, Focusable, ModalRoot, PanelSection, PanelSectionRow, Spinner } from "@decky/ui";
import { useEffect, useRef, useState } from "react";
import { DensePanel, PanelRow, SectionHeading } from "../components/PanelDensity";
import { ModalActions, modalActionStyle } from "../components/ModalActions";
import { describeError } from "../errors";
import { logUiFailure } from "../supportLog";
import type { PluginUpdateOperation } from "../types";

/** How often this asks the backend what the update it started is doing. */
const POLL_INTERVAL_MS = 1000;
/**
 * How long it goes on asking. The release archive is a little over a megabyte,
 * so this is patience for a slow connection rather than a deadline on anything:
 * the window stops reporting, and the durable record is what says how it ended.
 */
const POLL_ATTEMPTS = 180;
/**
 * How long this waits for Steam's interface to take the window away.
 *
 * Installing normally ends with the webhelper being replaced, which destroys
 * this window a few seconds later, so there is nothing to press and Back is
 * refused. If that restart never happens - the runner reports it as a request
 * that failed - refusing Back for ever would leave a window with no way out of
 * it at all, on a panel whose backend has already been replaced. After this the
 * way out comes back, and it says what it is and is not doing.
 */
const RESTART_GRACE_MS = 45_000;

interface Props {
  currentVersion: string;
  targetVersion: string;
  /** Whether a game is running right now, which the interface restart can displace. */
  gameRunning: boolean;
  /** Takes the version this window named, because that is what was confirmed. */
  onStart: (targetVersion: string) => Promise<PluginUpdateOperation>;
  onPoll: (operationId: string) => Promise<PluginUpdateOperation>;
  onCancelUpdate: (operationId: string) => Promise<PluginUpdateOperation>;
  onClose: () => void;
}

/**
 * The one confirmation an update gets, and then the only view of it there is.
 *
 * Three things have to be said before the press and none can be discovered
 * afterwards: which version replaces which, that Steam's own interface is
 * restarted as part of it, and that there is no way back once installing
 * starts. The last is why this window exists rather than the button acting
 * directly, and it is also why the window stays open afterwards: the download
 * is the only part of an update anything here can still report or stop.
 *
 * It stops watching at `installing` rather than on a failure. From that state
 * Decky is replacing this plugin, so the backend that would answer the next
 * question is being stopped and this panel is about to be replaced with it.
 */
export function UpdateModal(
  { currentVersion, targetVersion, gameRunning, onStart, onPoll, onCancelUpdate, onClose }: Props,
) {
  useUiSurface("UpdateModal");
  const [operation, setOperation] = useState<PluginUpdateOperation | null>(null);
  const [busy, setBusy] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const busyRef = useRef(false);
  const operationRef = useRef<string | null>(null);
  const liveRef = useRef(true);
  // Whether the backend stopped answering about an update it accepted. That is
  // the ordinary end of an install - Decky replaces this plugin - but it is
  // also what a failure looks like from here, and the two are the same silence.
  const [unreachable, setUnreachable] = useState(false);
  // Whether the interface restart this window is waiting for has taken longer
  // than it ever takes when it works.
  const [restartOverdue, setRestartOverdue] = useState(false);

  useEffect(() => () => { liveRef.current = false; }, []);

  const follow = async (operationId: string) => {
    let silent = 0;
    for (let attempt = 0; attempt < POLL_ATTEMPTS && liveRef.current; attempt += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, POLL_INTERVAL_MS));
      if (!liveRef.current) return;
      let snapshot: PluginUpdateOperation;
      try {
        snapshot = await onPoll(operationId);
      } catch (cause) {
        // The backend going away during its own replacement is the install
        // working. Anything else is a read that failed and is asked again.
        logUiFailure("update_modal.poll_failed", cause, { operation_id: operationId });
        silent += 1;
        // Past the point where an interface restart would have taken this
        // window away, silence is no longer something to wait out. The backend
        // can disappear between two of these reads, before it ever reported
        // `installing`, and the window then had no state to arm its own way out
        // with: it went on asking a plugin that no longer existed, with Back
        // refused, for as long as the user left it there.
        if (silent * POLL_INTERVAL_MS >= RESTART_GRACE_MS && liveRef.current) setUnreachable(true);
        continue;
      }
      if (!liveRef.current) return;
      silent = 0;
      setUnreachable(false);
      setOperation(snapshot);
      if (snapshot.state === "installing") return;
      if (snapshot.state === "failed" || snapshot.state === "cancelled") {
        busyRef.current = false;
        setBusy(false);
        setCancelling(false);
        return;
      }
    }
  };

  const confirm = async () => {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setError(null);
    const record = startUiOperation("update.start", { to_version: targetVersion });
    try {
      const started = await onStart(targetVersion);
      operationRef.current = started.operation_id;
      setOperation(started);
      record.completed();
      void follow(started.operation_id);
    } catch (cause) {
      record.failed(cause);
      setError(describeError(cause));
      busyRef.current = false;
      setBusy(false);
    }
  };

  const abandon = async () => {
    const operationId = operationRef.current;
    if (!operationId || cancelling) return;
    setCancelling(true);
    try {
      setOperation(await onCancelUpdate(operationId));
      busyRef.current = false;
      setBusy(false);
    } catch (cause) {
      setError(describeError(cause));
    } finally {
      setCancelling(false);
    }
  };

  const state = operation?.state ?? null;
  const installingNow = state === "installing";
  useEffect(() => {
    if (!installingNow) return;
    const timer = window.setTimeout(() => setRestartOverdue(true), RESTART_GRACE_MS);
    return () => window.clearTimeout(timer);
  }, [installingNow]);
  // Installing is the point of no return, and Back has to respect it as much as
  // the buttons do: there is nothing left here to abandon, and a window that
  // closes on its own reads as an update that stopped.
  const installing = installingNow && !restartOverdue;
  const inFlight = state === "checking" || state === "downloading";
  const settled = state === "failed" || state === "cancelled";
  // Handed over and out of view: the press was accepted, nothing here can be
  // told how it ended, and the way out has to come back rather than the window
  // waiting for an answer that has no one left to give it.
  const stranded = unreachable && !settled && !installingNow;

  return (
    <ModalRoot onCancel={traceUiAction("update_modal.cancel_back", () => { if (stranded || (!busyRef.current && !installing)) onClose(); })}>
      <Focusable style={{ minWidth: 420, maxWidth: 600 }}>
        <DensePanel>
          <PanelSection>
            <SectionHeading>Update CE Decky</SectionHeading>
            <PanelSectionRow>
              <PanelRow
                tone="header"
                testId="update-versions"
                label={`v${currentVersion} to v${targetVersion}`}
                description="The release is downloaded, checked against the checksum the release itself publishes, and installed by Decky."
              />
            </PanelSectionRow>
            <PanelSectionRow>
              <PanelRow
                status
                testId="update-restart-warning"
                label="Steam's interface restarts"
                description={gameRunning
                  ? "Installing replaces Steam's interface process. This closes the Decky panel and can interrupt the game that is running."
                  : "Installing replaces Steam's interface process, which closes the Decky panel for a few seconds."}
              />
            </PanelSectionRow>
            <PanelSectionRow>
              <PanelRow
                status
                testId="update-no-cancel"
                label="It cannot be cancelled once it starts installing"
                description="Downloading can be stopped. From the moment Decky begins replacing the plugin there is nothing left here to stop it, because this panel is part of what is being replaced."
              />
            </PanelSectionRow>
            {operation && (
              <PanelSectionRow>
                <PanelRow
                  truncate
                  testId="update-progress"
                  label={installing ? "Installing" : operation.state.replace(/_/g, " ")}
                  description={operation.error ?? operation.message}
                  trailing={inFlight || installing ? <Spinner style={{ width: 14, height: 14 }} /> : undefined}
                />
              </PanelSectionRow>
            )}
            {/* Once Decky has it there is nothing left to press, so the window
                says what is happening where everything else on it is said. It
                used to say it in the row the actions live in, which is one
                horizontal group of controls and not a place for a sentence. */}
            {installing && (
              <PanelSectionRow>
                <PanelRow
                  status
                  testId="update-installing-note"
                  label="Steam's interface is restarting"
                  description="This window closes with it. CE Decky reports what happened once the panel comes back."
                />
              </PanelSectionRow>
            )}
            {stranded && (
              <PanelSectionRow>
                <PanelRow
                  status
                  testId="update-handed-off"
                  label="CE Decky stopped answering"
                  description="The update was accepted and this panel can no longer be told how it ended, which is what an install that is replacing the plugin looks like from here. Closing this window does not stop it; CE Decky reports what happened in Advanced, under Plugin updates."
                />
              </PanelSectionRow>
            )}
            {installingNow && restartOverdue && (
              <PanelSectionRow>
                <PanelRow
                  status
                  testId="update-restart-overdue"
                  label="Steam's interface has not restarted"
                  description="The update was handed to Decky and is finishing on its own. Closing this window does not stop it; CE Decky reports what happened in Advanced, under Plugin updates."
                />
              </PanelSectionRow>
            )}
            {error && (
              <PanelSectionRow>
                <PanelRow testId="update-modal-error" label="The update could not be started" description={error} />
              </PanelSectionRow>
            )}
          </PanelSection>
        </DensePanel>
        {((installingNow && restartOverdue) || stranded) && (
          <ModalActions>
            <DialogButton
              style={modalActionStyle}
              onClick={traceUiAction("update_modal.close_overdue", () => onClose())}
            >
              Close
            </DialogButton>
          </ModalActions>
        )}
        {!installingNow && !stranded && (
          <ModalActions>
            <DialogButton
              style={modalActionStyle}
              disabled={cancelling}
              onClick={traceUiAction("update_modal.not_now", () => {
                if (inFlight) { void abandon(); return; }
                if (!busyRef.current) onClose();
              })}
            >
              {inFlight ? (cancelling ? "Stopping…" : "Stop") : "Not now"}
            </DialogButton>
            <DialogButton
              style={modalActionStyle}
              disabled={busy && !settled}
              onClick={traceUiAction("update_modal.update", () => { void confirm(); }, { to_version: targetVersion })}
            >
              {settled ? "Try again" : busy ? "Starting…" : "Update"}
            </DialogButton>
          </ModalActions>
        )}
      </Focusable>
    </ModalRoot>
  );
}
