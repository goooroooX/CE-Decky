import { describe, expect, it } from "vitest";
import { describeError, pythonTracebackSummary } from "../src/errors";

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

  it("reads the exception line of a traceback and ignores its frames", () => {
    expect(pythonTracebackSummary(TRACEBACK))
      .toBe("existing managed CE runtime is inconsistent; do not overwrite immutable runtime state");
    expect(pythonTracebackSummary("Traceback (most recent call last):\n  File \"x\", line 1\nKeyError\n"))
      .toBe("KeyError");
    expect(pythonTracebackSummary(null)).toBe(null);
    expect(pythonTracebackSummary("")).toBe(null);
  });
});
