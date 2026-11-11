import React from "react";
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  PENDING_COMPATIBILITY_ATTEMPTS,
  PENDING_COMPATIBILITY_DELAY_MS,
  usePendingCompatibilityRefresh,
} from "../src/pendingCompatibility";
import type { CompatibilityEvidence } from "../src/types";

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const row = (pending: boolean): CompatibilityEvidence => ({
  app_id: 10, table_sha256: "a".repeat(64), target_process: "game.exe", pe_version: null, steam_build_id: null,
  last_working_at: 1, invalidated: false, state: pending ? "unknown" : "matching",
  comparison: "executable-sha256", pending,
});

function Screen({ entries, refresh }: { entries: CompatibilityEvidence[]; refresh: () => Promise<unknown> }) {
  usePendingCompatibilityRefresh(entries, refresh, "test");
  return null;
}

it("asks again while a comparison is pending and stops once it is not", async () => {
  const refresh = vi.fn(async () => undefined);
  const view = render(<Screen entries={[row(true)]} refresh={refresh} />);
  await act(async () => { vi.advanceTimersByTime(PENDING_COMPATIBILITY_DELAY_MS); });
  expect(refresh).toHaveBeenCalledTimes(1);
  // The answer arrives as a new list, still pending: one more ask.
  view.rerender(<Screen entries={[row(true)]} refresh={refresh} />);
  await act(async () => { vi.advanceTimersByTime(PENDING_COMPATIBILITY_DELAY_MS); });
  expect(refresh).toHaveBeenCalledTimes(2);
  // Settled: nothing more is scheduled, however long the screen stays open.
  view.rerender(<Screen entries={[row(false)]} refresh={refresh} />);
  await act(async () => { vi.advanceTimersByTime(PENDING_COMPATIBILITY_DELAY_MS * 10); });
  expect(refresh).toHaveBeenCalledTimes(2);
});

it("gives up after its bound, and a screen that closes takes its timer with it", async () => {
  const refresh = vi.fn(async () => undefined);
  const view = render(<Screen entries={[row(true)]} refresh={refresh} />);
  for (let index = 0; index < PENDING_COMPATIBILITY_ATTEMPTS + 5; index += 1) {
    await act(async () => { vi.advanceTimersByTime(PENDING_COMPATIBILITY_DELAY_MS); });
    view.rerender(<Screen entries={[row(true)]} refresh={refresh} />);
  }
  expect(refresh).toHaveBeenCalledTimes(PENDING_COMPATIBILITY_ATTEMPTS);

  const closing = vi.fn(async () => undefined);
  const other = render(<Screen entries={[row(true)]} refresh={closing} />);
  other.unmount();
  await act(async () => { vi.advanceTimersByTime(PENDING_COMPATIBILITY_DELAY_MS * 2); });
  expect(closing).not.toHaveBeenCalled();
});

it("asks again after an ask that failed, within the same bound", async () => {
  // A failed ask brings no new list, so nothing else would schedule the next.
  const refresh = vi.fn()
    .mockRejectedValueOnce(new Error("backend busy"))
    .mockResolvedValue(undefined);
  render(<Screen entries={[row(true)]} refresh={refresh} />);
  await act(async () => { vi.advanceTimersByTime(PENDING_COMPATIBILITY_DELAY_MS); });
  await act(async () => { vi.advanceTimersByTime(PENDING_COMPATIBILITY_DELAY_MS); });
  expect(refresh).toHaveBeenCalledTimes(2);

  const failing = vi.fn(async () => { throw new Error("gone"); });
  const view = render(<Screen entries={[row(true)]} refresh={failing} />);
  for (let index = 0; index < PENDING_COMPATIBILITY_ATTEMPTS + 5; index += 1) {
    await act(async () => { vi.advanceTimersByTime(PENDING_COMPATIBILITY_DELAY_MS); });
  }
  expect(failing).toHaveBeenCalledTimes(PENDING_COMPATIBILITY_ATTEMPTS);
  // A different comparison pending is asked about from the start.
  view.rerender(<Screen entries={[{ ...row(true), table_sha256: "b".repeat(64) }]} refresh={failing} />);
  await act(async () => { vi.advanceTimersByTime(PENDING_COMPATIBILITY_DELAY_MS); });
  expect(failing).toHaveBeenCalledTimes(PENDING_COMPATIBILITY_ATTEMPTS + 1);
});
