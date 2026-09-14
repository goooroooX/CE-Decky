import { afterEach, describe, expect, it, vi } from "vitest";

import { elapsedSince, monotonicNow } from "../src/elapsed";

/**
 * The clock every bounded wait in the panel measures with.
 *
 * These are about one property: a wall-clock correction, which this project
 * already handles in its persisted install timestamps and in the Search marker
 * it loads, must not change how long a bounded wait lasts. A wait that grows
 * with a backwards correction holds the panel's busy latch for as long as it
 * lasts, and one that ends with a forwards correction reports work that was
 * still running as gone.
 */
describe("the panel's elapsed clock", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("does not move when the wall clock is corrected in either direction", () => {
    const performanceNow = vi.spyOn(performance, "now");
    performanceNow.mockReturnValue(1_000);
    const started = monotonicNow();

    vi.spyOn(Date, "now").mockReturnValue(0);
    performanceNow.mockReturnValue(13_000);
    expect(elapsedSince(started)).toBe(12_000);

    vi.spyOn(Date, "now").mockReturnValue(4_000_000_000_000);
    expect(elapsedSince(started)).toBe(12_000);
  });

  it("never reports a negative duration", () => {
    const performanceNow = vi.spyOn(performance, "now").mockReturnValue(500);
    const started = monotonicNow();
    performanceNow.mockReturnValue(400);

    expect(elapsedSince(started)).toBe(0);
  });

  it("falls back to the wall clock rather than throwing where there is no monotonic one", () => {
    // The packaging smoke check loads this bundle in bare Node, and a
    // diagnostics-grade wait that throws would be worse than one that can be
    // skewed. Every host this panel actually runs on has `performance.now`.
    const host = globalThis as { performance?: unknown };
    const real = host.performance;
    host.performance = undefined;
    try {
      vi.spyOn(Date, "now").mockReturnValue(1_700_000_000_000);
      expect(monotonicNow()).toBe(1_700_000_000_000);
    } finally {
      host.performance = real;
    }
  });
});
