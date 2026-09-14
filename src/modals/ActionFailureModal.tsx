import { useUiSurface } from "../useUiSurface";
import { traceUiAction } from "../uiActions";
import { DialogButton, Focusable, ModalRoot, PanelSection, showModal } from "@decky/ui";
import { DensePanel, PanelRow, SectionHeading } from "../components/PanelDensity";
import { ModalActions, modalActionStyle } from "../components/ModalActions";
import { describeError, pythonTracebackSummary } from "../errors";
import { logUi } from "../supportLog";

interface Props {
  /** What the user was doing, in their terms. */
  subject: string;
  message: string;
  /** What survived the RPC boundary and is worth reading second. */
  details: string | null;
  onClose: () => void;
}

/**
 * What failed, where the user can actually read it.
 *
 * Every failed press used to go to a Steam notification, which is a banner that
 * slides away on its own timer: on a handheld the user is holding, the whole
 * message is gone before it has been read, and the only place the text still
 * existed was a panel row that the workflow had usually already replaced. A
 * dialog stays until it is closed, which is the point.
 *
 * The backend's own detail is kept on a second row rather than folded into the
 * sentence. A Python exception class and its last frame are what a bug report
 * needs and are noise to everyone else, and this screen is read by both.
 */
export function ActionFailureModal({ subject, message, details, onClose }: Props) {
  useUiSurface("ActionFailureModal");
  return (
    <ModalRoot onCancel={traceUiAction("action_failure_modal.on_close", onClose)}>
      <Focusable style={{ minWidth: 440, maxWidth: 680 }}>
        <DensePanel>
          <PanelSection>
            <SectionHeading>Something did not work</SectionHeading>
            <PanelRow tone="header" testId="action-failure" label={subject} description={message} />
            {details ? <PanelRow truncate testId="action-failure-details" label="Details" description={details} /> : null}
          </PanelSection>
        </DensePanel>
        <ModalActions>
          <DialogButton style={modalActionStyle} onClick={traceUiAction("action_failure_modal.close", onClose)}>Close</DialogButton>
        </ModalActions>
      </Focusable>
    </ModalRoot>
  );
}

/**
 * Show one failed action, and record it where a support bundle will find it.
 *
 * Returns whether the dialog opened. Steam can refuse to open one, and a
 * failure that cannot be shown must still reach the caller's own fallback
 * rather than being swallowed by the reporting of it.
 */
export function showActionFailure(subject: string, cause: unknown): boolean {
  // One failure, one dialog. A press routinely crosses two of these boundaries:
  // the panel's own `runAction` reports and rethrows, and the search screen that
  // called it reports again, so a failed import or a failed Clear opened two
  // dialogs the user had to dismiss in turn. Marking the cause is exact where
  // counting open dialogs would not be: two genuinely different failures at the
  // same moment still each get one. A primitive thrown value cannot be marked
  // and is shown, which is the safe direction.
  if (typeof cause === "object" && cause !== null) {
    const marked = cause as { ceDeckyFailureShown?: boolean };
    if (marked.ceDeckyFailureShown === true) return true;
  }
  const message = describeError(cause);
  const traceback = (cause as { pythonTraceback?: unknown })?.pythonTraceback;
  const details = pythonTracebackSummary(traceback);
  try {
    // Recorded as one line, not as a second copy of the failure: every caller
    // has already written the cause and its traceback through `logUiFailure`,
    // and the ring buffer this shares is bounded at 500 entries.
    logUi("panel.failure_shown", { subject });
    let close = () => undefined as void;
    const handle = showModal(
      <ActionFailureModal
        subject={subject}
        message={message}
        details={details && details !== message ? details : null}
        onClose={() => close()}
      />,
    );
    close = () => handle.Close();
    // Only once the dialog is actually up: a `showModal` Steam refused must
    // leave the caller's own fallback, and the next reporter of the same cause,
    // free to say something.
    if (typeof cause === "object" && cause !== null) {
      try {
        Object.defineProperty(cause, "ceDeckyFailureShown", {
          value: true, enumerable: false, configurable: true, writable: true,
        });
      } catch {
        // A frozen thrown value cannot be marked; the worst case is the pair of
        // dialogs this avoids, which is not worth failing the report over.
      }
    }
    return true;
  } catch {
    return false;
  }
}
