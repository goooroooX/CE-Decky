import { describe, expect, it } from "vitest";
import { MAX_PROMOTED_CAUSE, causeAsLabel, describeError, leadWithCause, pythonTracebackSummary } from "../src/errors";

/**
 * Decky Loader 3.2.6 builds `new PyError(name, data.error.error, traceback)`
 * while the backend sends `{name, message, traceback}`, so `message` is always
 * the empty string and only the traceback still carries the Python text.
 */
class PyErrorLike extends Error {
  pythonTraceback: string | null;

  constructor(name: string, traceback: string | null) {
    super(undefined as unknown as string);
    this.name = `Python ${name}`;
    this.pythonTraceback = traceback;
  }
}

const TRACEBACK = [
  "Traceback (most recent call last):",
  '  File "/home/deck/homebrew/plugins/CE-Decky/py_modules/ce_decky/service.py", line 927, in prepare_private_ce_runtime',
  "    runtime = materialize_private_runtime(",
  '  File "/home/deck/homebrew/plugins/CE-Decky/py_modules/ce_decky/ce_runtime.py", line 154, in materialize_private_runtime',
  "    raise ValueError(",
  "ValueError: existing managed CE runtime is inconsistent; do not overwrite immutable runtime state",
  "",
].join("\n");

describe("describeError", () => {
  it("recovers the backend message the Decky loader drops", () => {
    const cause = new PyErrorLike("ValueError", TRACEBACK);
    expect(cause.message).toBe("");
    expect(describeError(cause)).toBe(
      "existing managed CE runtime is inconsistent; do not overwrite immutable runtime state",
    );
  });

  it("names the exception class when even the traceback is unavailable", () => {
    expect(describeError(new PyErrorLike("RuntimeError", null)))
      .toBe("RuntimeError (the backend reported no message)");
  });

  it("prefers a real JavaScript message", () => {
    expect(describeError(new Error("Another CE Decky operation is still running.")))
      .toBe("Another CE Decky operation is still running.");
  });

  it("never returns an empty or opaque notification body", () => {
    expect(describeError(new Error(""))).toBe("CE Decky failed without reporting a reason.");
    expect(describeError(undefined)).toBe("CE Decky failed without reporting a reason.");
    expect(describeError(null)).toBe("CE Decky failed without reporting a reason.");
    expect(describeError({})).toBe("CE Decky failed without reporting a reason.");
    expect(describeError(new Error(""), "Cheat Engine could not start.")).toBe("Cheat Engine could not start.");
  });

  it("passes through plain string and non-Error rejections", () => {
    expect(describeError("resident bridge is disconnected")).toBe("resident bridge is disconnected");
    expect(describeError(404)).toBe("404");
  });

  it("puts the finding first, where a truncated row will still show it", () => {
    // A panel row is cut to one line, and the first forty characters or so are
    // what a narrow panel is certain to show. Opening with our own framing
    // spends them on what the label above already said.
    const line = leadWithCause("Cheat Engine refused to open the table", "Stop it and start it again.");
    expect(line).toBe("Cheat Engine refused to open the table. Stop it and start it again.");
    expect(line.slice(0, 40)).toContain("Cheat Engine refused to open the table");
    // A cause that is already a sentence is not given a second full stop, and
    // one the backend never wrote leaves our own line standing alone.
    expect(leadWithCause("The bridge timed out.", "Try again.")).toBe("The bridge timed out. Try again.");
    expect(leadWithCause(null, "Try again.")).toBe("Try again.");
    expect(leadWithCause("   ", "Try again.")).toBe("Try again.");
    expect(leadWithCause("read\n  timed out", "Try again.")).toBe("read timed out. Try again.");
  });

  it("promotes only a cause short enough to be a label", () => {
    expect(causeAsLabel("Cheat Engine refused to open the table.")).toBe("Cheat Engine refused to open the table");
    expect(causeAsLabel(" the address list is empty ")).toBe("the address list is empty");
    expect(causeAsLabel("x".repeat(MAX_PROMOTED_CAUSE))).toBe("x".repeat(MAX_PROMOTED_CAUSE));
    // Longer than a label can carry, so it stays in the description, where the
    // row can be opened to read the whole of it.
    expect(causeAsLabel("x".repeat(MAX_PROMOTED_CAUSE + 1))).toBe(null);
    expect(causeAsLabel(null)).toBe(null);
    expect(causeAsLabel("")).toBe(null);
  });

  it("reads the exception line of a traceback and ignores its frames", () => {
    expect(pythonTracebackSummary(TRACEBACK))
      .toBe("existing managed CE runtime is inconsistent; do not overwrite immutable runtime state");
    expect(pythonTracebackSummary("Traceback (most recent call last):\n  File \"x\", line 1\nKeyError\n"))
      .toBe("KeyError");
    expect(pythonTracebackSummary(null)).toBe(null);
    expect(pythonTracebackSummary("")).toBe(null);
  });
});
