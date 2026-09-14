import { logUi, logUiFailure } from "./supportLog";
import { elapsedSince, monotonicNow } from "./elapsed";

export interface UiActionContext { action: string; interaction: number }
type Fields = Readonly<Record<string, string | number | boolean | null | undefined>>;
let sequence = 0;
let current: UiActionContext | undefined;
let lastEdit: { key: string; at: number } | undefined;

/** Capture at an operation's entry, before its first await. Never a global async context. */
export function currentUiAction(): UiActionContext | undefined { return current; }

/** The owner calls exactly one terminal method after its actual work settles. */
export function startUiOperation(operation: string, fields: Fields = {}) {
  const context = { ...current, operation, ...fields };
  const started = monotonicNow();
  logUi("ui.operation_started", context);
  return {
    completed: (result: Fields = {}) => logUi("ui.operation_completed", { ...context, ...result, duration_ms: elapsedSince(started) }),
    failed: (cause: unknown, result: Fields = {}) => logUiFailure("ui.operation_failed", cause, { ...context, ...result, duration_ms: elapsedSince(started) }),
  };
}

/**
 * Trace the actual controller/mouse callback, including navigation and dismissals.
 * Arguments are deliberately never inspected: they may contain passwords, table
 * code, typed values or provider URLs. Callers supply only safe identities.
 * This records a request, not a successful mutation. The operation that owns
 * the async work records its outcome, even when this callback returns void.
 */
export function traceUiAction<Args extends any[], Result>(
  action: string,
  handler: (...args: Args) => Result,
  fields: Fields | ((...args: Args) => Fields) = {},
): (...args: Args) => Result {
  return (...args) => {
    const previous = current;
    lastEdit = undefined;
    const context = { action, interaction: ++sequence };
    current = context;
    try {
      logUi("ui.action", { ...(typeof fields === "function" ? fields(...args) : fields), ...context });
      const result = handler(...args);
      // Keep the original return value and rejection for the caller. Observe
      // promises too: a detached async callback may have no outer error owner.
      if (result && typeof (result as unknown as PromiseLike<unknown>).then === "function") {
        void Promise.resolve(result).catch((cause) => logUiFailure("ui.handler_failed", cause, context));
      }
      return result;
    } catch (cause) {
      logUiFailure("ui.handler_failed", cause, context);
      throw cause;
    } finally {
      current = previous;
    }
  };
}

/** Log an editing burst, never each keystroke or the field's value. */
export function traceUiEdit<Args extends any[], Result>(
  action: string, handler: (...args: Args) => Result, fields: Fields = {},
): (...args: Args) => Result {
  return (...args) => {
    const key = JSON.stringify([action, fields]);
    const now = monotonicNow();
    if (!lastEdit || lastEdit.key !== key || now - lastEdit.at > 1500) {
      logUi("ui.edit", { ...fields, action, interaction: ++sequence });
    }
    lastEdit = { key, at: now };
    try {
      return handler(...args);
    } catch (cause) {
      logUiFailure("ui.handler_failed", cause, { action });
      throw cause;
    }
  };
}
