import { useUiSurface } from "../useUiSurface";
import { traceUiAction } from "../uiActions";
import { DialogButton, Focusable, ModalRoot, PanelSection, showModal } from "@decky/ui";
import { DensePanel, PanelRow, SectionHeading } from "../components/PanelDensity";
import { ModalActions, modalActionStyle } from "../components/ModalActions";
import { describeError, pythonTracebackSummary } from "../errors";
import { logUi, logUiWarning } from "../supportLog";
import type { RefusedActivation } from "../runtimeClient";

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

/**
 * How many refused cheats are named one per row before the rest are counted.
 *
 * The rows hold no control, so nothing scrolls this window from a controller,
 * and a Steam Deck gives a modal 534 CSS pixels, so the list stays short enough
 * to leave the heading, the sentence above it and Close on that screen.
 */
const REFUSED_ROWS = 5;

/**
 * The cheats one Apply could not switch on, while the rest of it worked.
 *
 * Not the question about the table. That question asks whether to stop using a
 * table, and it was put on the device after a press in which one cheat of
 * several was refused and the rest went on: a table that works, offered for
 * retirement because of one of its cheats. What the reader can do about a
 * single refused cheat is leave it off, so this says which ones and why.
 */
export function CheatsNotSwitchedOnModal({ refused, onClose }: { refused: readonly RefusedActivation[]; onClose: () => void }) {
  useUiSurface("CheatsNotSwitchedOnModal");
  const shown = refused.slice(0, REFUSED_ROWS);
  const more = refused.length - shown.length;
  return (
    <ModalRoot onCancel={traceUiAction("cheats_not_switched_on_modal.on_close", onClose)}>
      <Focusable style={{ minWidth: 440, maxWidth: 680 }}>
        <DensePanel>
          <PanelSection>
            <SectionHeading>{refused.length === 1 ? "One cheat did not switch on" : `${refused.length} cheats did not switch on`}</SectionHeading>
            <PanelRow
              tone="header"
              testId="cheats-not-switched-on"
              label="Everything else you applied is on"
              description="These stay off, and are remembered off for the next time this game starts. One that went straight back off usually could not find what it looks for in this build of the game, which is about that cheat rather than the whole table."
            />
            {shown.map((item) => (
              <PanelRow key={item.record_id} truncate testId={`cheat-not-switched-on-${item.record_id}`} label={item.label} description={item.reason} />
            ))}
            {more > 0 ? <PanelRow testId="cheats-not-switched-on-more" label={`And ${more} more`} description="Each is off, for one of the reasons above." /> : null}
          </PanelSection>
        </DensePanel>
        <ModalActions>
          <DialogButton style={modalActionStyle} onClick={traceUiAction("cheats_not_switched_on_modal.close", onClose)}>Close</DialogButton>
        </ModalActions>
      </Focusable>
    </ModalRoot>
  );
}

/** Show what one Apply could not switch on. Returns whether the dialog opened. */
export function showCheatsNotSwitchedOn(refused: readonly RefusedActivation[]): boolean {
  if (refused.length === 0) return true;
  try {
    let close = () => undefined as void;
    const handle = showModal(<CheatsNotSwitchedOnModal refused={refused} onClose={() => close()} />);
    close = () => handle.Close();
    return true;
  } catch {
    logUiWarning("cheats.not_switched_on_unshown", { count: refused.length });
    return false;
  }
}
