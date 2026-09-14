import { afterEach, expect, it, vi } from "vitest";
import { ManagedSetupOwner } from "../src/managedSetup";
import type { ManagedCECapability, ManagedCEInstallStatus } from "../src/types";

const active = { operation_id: "1".repeat(32), state: "extracting", message: "Extracting", error: null, progress: null, installed: null, provenance: null } satisfies ManagedCEInstallStatus;
const capability = { operation: active, release: null } as ManagedCECapability;
function fixture() {
  const aborter = new AbortController();
  const deps = {
    capability: vi.fn().mockResolvedValue(capability), status: vi.fn(), start: vi.fn(),
    poll: vi.fn().mockResolvedValue(active), cancel: vi.fn().mockResolvedValue({ ...active, state: "cancelled" }),
    complete: vi.fn(), publish: vi.fn(), completed: vi.fn(), signal: aborter.signal,
  };
  return { aborter, deps, owner: new ManagedSetupOwner(deps) };
}
afterEach(() => vi.useRealTimers());

it.each([false, true])("reconciles a missed terminal poll by exact completion receipt (handoff=%s)", async (handoff) => {
  vi.useFakeTimers();
  const { owner, deps } = fixture();
  const b = { ...active, operation_id: "2".repeat(32) };
  const receipt = { executable: "/managed/CE.exe", root: "/managed", sha256: "a".repeat(64), size: 1, completed_now: false };
  deps.complete.mockResolvedValue(receipt);
  deps.capability.mockResolvedValue({ ...capability, operation: null });
  deps.poll.mockRejectedValue(new Error("operation is unknown"));
  if (handoff) {
    deps.poll.mockResolvedValueOnce({ ...active, state: "cancelled" });
    deps.capability.mockResolvedValueOnce({ ...capability, operation: b });
  }
  const run = owner.run(true, capability);
  await vi.advanceTimersByTimeAsync(handoff ? 1000 : 500);
  await run;
  expect(deps.complete).toHaveBeenCalledOnce();
  expect(deps.complete).toHaveBeenCalledWith(handoff ? b.operation_id : active.operation_id);
  expect(deps.publish).toHaveBeenLastCalledWith(null);
  expect(deps.publish.mock.calls.some(([op]) => op?.state === "failed")).toBe(false);
  expect(deps.completed).toHaveBeenCalledOnce();
  expect(deps.completed).toHaveBeenCalledWith(receipt, false);
});

it("preserves Cancel's completed receipt for A while adopting B", async () => {
  vi.useFakeTimers();
  const { owner, deps } = fixture();
  const b = { ...active, operation_id: "2".repeat(32) };
  deps.cancel.mockResolvedValue({ ...active, state: "completed" });
  deps.complete.mockRejectedValue(new Error("managed CE setup changed; refresh before completing it"));
  deps.capability.mockResolvedValueOnce({ ...capability, operation: b }).mockResolvedValue({ ...capability, operation: { ...b, state: "cancelled" } });
  deps.poll.mockResolvedValue({ ...b, state: "cancelled" });
  const run = owner.run(false, capability);
  const cancel = owner.cancel();
  const outcome = cancel.then((value) => value, (cause) => cause);
  await vi.advanceTimersByTimeAsync(2000);
  expect(await outcome).toBe("completed");
  await run;
  expect(deps.poll).toHaveBeenCalledWith(b.operation_id);
  expect(deps.publish).toHaveBeenLastCalledWith(expect.objectContaining({ operation_id: b.operation_id }));
});

it.each([false, true])("lets Cancel preempt unavailable capability reconciliation (pending read=%s)", async (pending) => {
  vi.useFakeTimers();
  const { owner, deps } = fixture();
  deps.poll.mockRejectedValue(new Error("poll failed"));
  deps.capability.mockRejectedValue(new Error("capability unavailable"));
  if (pending) deps.capability.mockImplementationOnce(() => new Promise(() => undefined));
  const run = owner.run(false, capability);
  await vi.advanceTimersByTimeAsync(500);
  expect(deps.capability).toHaveBeenCalled();
  const cancellation = owner.cancel();
  // No recovery of capability and no 8-second read deadline is required.
  await vi.advanceTimersByTimeAsync(0);
  await expect(cancellation).resolves.toBe("cancelled");
  await run;
  expect(deps.publish).toHaveBeenLastCalledWith(expect.objectContaining({ state: "cancelled" }));
  expect(vi.getTimerCount()).toBe(0);
});

it("still reconciles a failed cancellation receipt before declaring its outcome", async () => {
  vi.useFakeTimers();
  const { owner, deps, aborter } = fixture();
  const cause = new Error("cancel receipt lost");
  deps.poll.mockRejectedValue(new Error("poll failed"));
  deps.capability.mockRejectedValue(new Error("capability unavailable"));
  deps.cancel.mockRejectedValue(cause);
  const run = owner.run(false, capability);
  await vi.advanceTimersByTimeAsync(500);
  const cancelled = owner.cancel();
  const settled = vi.fn();
  const outcome = cancelled.then(settled, (error) => { settled(); return error; });
  await vi.advanceTimersByTimeAsync(1000);
  expect(settled).not.toHaveBeenCalled();
  deps.capability.mockResolvedValue(capability);
  await vi.advanceTimersByTimeAsync(1500);
  expect(await outcome).toBe(cause);
  aborter.abort();
  await run;
});

it.each(["cancelled", "completed"])("owns a slow cancellation until its actual %s receipt", async (state) => {
  vi.useFakeTimers();
  const { owner, deps } = fixture();
  let finish!: (value: unknown) => void;
  deps.cancel.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
  deps.complete.mockResolvedValue({ completed_now: true });
  const run = owner.run(false, capability);
  const cancellation = owner.cancel();
  const settled = vi.fn();
  void cancellation.then(settled, settled);
  await vi.advanceTimersByTimeAsync(20000);
  expect(settled).not.toHaveBeenCalled();
  expect(deps.cancel).toHaveBeenCalledOnce();
  expect(deps.capability).not.toHaveBeenCalled();
  finish({ ...active, state });
  await expect(cancellation).resolves.toBe(state);
  await run;
  expect(settled).toHaveBeenCalledOnce();
});

it("owns a delayed start even when no backend operation is visible before its receipt", async () => {
  vi.useFakeTimers();
  const { owner, deps } = fixture();
  const empty = { ...capability, operation: null };
  deps.capability.mockResolvedValue(empty);
  deps.poll.mockResolvedValue({ ...active, state: "cancelled" });
  let finish!: (value: unknown) => void;
  deps.start.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
  const run = owner.run(false, empty);
  const settled = vi.fn();
  void run.then(settled, settled);
  await vi.advanceTimersByTimeAsync(20000);
  expect(settled).not.toHaveBeenCalled();
  expect(deps.capability).not.toHaveBeenCalled();
  finish(active);
  await vi.advanceTimersByTimeAsync(500);
  await run;
  expect(deps.poll).toHaveBeenCalledWith(active.operation_id);
  expect(deps.start).toHaveBeenCalledOnce();
});

it("does not retry an unresolved completion mutation after a read deadline", async () => {
  vi.useFakeTimers();
  const { owner, deps } = fixture();
  let finish!: (value: unknown) => void;
  deps.complete.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
  const completed = { ...active, state: "completed" };
  deps.capability.mockResolvedValue({ ...capability, operation: completed });
  const run = owner.run(false, { ...capability, operation: completed });
  await vi.advanceTimersByTimeAsync(20000);
  expect(deps.complete).toHaveBeenCalledOnce();
  expect(deps.capability).not.toHaveBeenCalled();
  finish({ completed_now: true });
  await run;
  expect(deps.completed).toHaveBeenCalledOnce();
});

it.each(["failed", "cancelled", "completed"])("hands terminal %s A to authoritative operation B without repainting A", async (state) => {
  vi.useFakeTimers();
  const { owner, deps } = fixture();
  const terminalA = { ...active, state, error: state === "failed" ? "A failed" : null };
  const b = { ...active, operation_id: "2".repeat(32) };
  const terminalB = { ...b, state: "cancelled" };
  deps.poll.mockResolvedValueOnce(terminalA).mockResolvedValue(terminalB);
  deps.complete.mockResolvedValue({ completed_now: true });
  deps.capability.mockResolvedValueOnce({ ...capability, operation: b }).mockResolvedValue({ ...capability, operation: terminalB });
  const run = owner.run(false, capability);
  await vi.advanceTimersByTimeAsync(1000);
  await run;
  expect(deps.poll.mock.calls.map(([id]) => id)).toEqual([active.operation_id, b.operation_id]);
  const observations = deps.publish.mock.calls.map(([op]) => op);
  const adopted = observations.findIndex((op) => op?.operation_id === b.operation_id);
  expect(adopted).toBeGreaterThan(-1);
  expect(observations.slice(adopted).every((op) => op?.operation_id !== active.operation_id)).toBe(true);
  expect(deps.publish).toHaveBeenLastCalledWith(terminalB);
});

it.each(["failed", "cancelled"])("keeps a fresh start refusal instead of the previous %s operation's error", async (state) => {
  const { owner, deps } = fixture();
  const previous = { ...capability, operation: { ...active, state, error: "old network failure" } };
  const cause = new Error("current live CE refuses setup");
  deps.start.mockRejectedValue(cause);
  deps.capability.mockResolvedValue(previous);
  await expect(owner.run(false, previous)).rejects.toBe(cause);
  expect(deps.poll).not.toHaveBeenCalled();
  expect(deps.publish).not.toHaveBeenCalled();
});

it("ignores a late poll after cancellation and never consumes its stale completion", async () => {
  vi.useFakeTimers();
  const { owner, deps } = fixture();
  let finishPoll!: (value: unknown) => void;
  deps.poll.mockImplementationOnce(() => new Promise((resolve) => { finishPoll = resolve; }));
  const run = owner.run(false, capability);
  await vi.advanceTimersByTimeAsync(500);
  const cancelled = owner.cancel();
  await expect(cancelled).resolves.toBe("cancelled");
  await run;
  finishPoll({ ...active, state: "completed" });
  await vi.advanceTimersByTimeAsync(0);
  expect(deps.complete).not.toHaveBeenCalled();
  expect(deps.publish).toHaveBeenLastCalledWith(expect.objectContaining({ state: "cancelled" }));
});

it("keeps monitoring after reconciliation itself is temporarily unavailable", async () => {
  vi.useFakeTimers();
  const { owner, deps } = fixture();
  deps.poll.mockRejectedValueOnce(new Error("lost poll")).mockResolvedValue({ ...active, state: "cancelled" });
  deps.capability.mockRejectedValueOnce(new Error("lost capability"));
  const run = owner.run(false, capability);
  await vi.advanceTimersByTimeAsync(2000);
  await run;
  expect(deps.poll).toHaveBeenCalledTimes(2);
  expect(deps.start).not.toHaveBeenCalled();
  expect(deps.publish).toHaveBeenLastCalledWith(expect.objectContaining({ state: "cancelled" }));
});

it("releases an unmounted observer without cancelling the backend operation", async () => {
  vi.useFakeTimers();
  const { owner, deps, aborter } = fixture();
  deps.poll.mockImplementationOnce(() => new Promise(() => undefined));
  const run = owner.run(false, capability);
  await vi.advanceTimersByTimeAsync(500);
  aborter.abort();
  await run;
  expect(deps.cancel).not.toHaveBeenCalled();
  expect(vi.getTimerCount()).toBe(0);
});
