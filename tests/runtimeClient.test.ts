import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  getRuntimeStatus: vi.fn(),
  writeRuntimeCommands: vi.fn(),
  confirmTableWorking: vi.fn().mockResolvedValue(true),
}));
vi.mock("../src/api", () => api);

import { MAX_LIVE_CONTROLS, RuntimeOperationError, RuntimeOutcomeUnknownError, RuntimeQueryAbortedError, applyRuntimeSelection, deactivateAllActiveControls, queryReadingInertScripts, queryRuntimeControls, queryRuntimeControlsPartial, sendRuntimeCommandAndWait } from "../src/runtimeClient";

const prepared = {
  session_id: "session",
  app_id: 10,
  ce_sha256: "a".repeat(64),
  table_sha256: "b".repeat(64),
  descriptor_path: "/d",
  descriptor_sha256: "c".repeat(64),
  descriptor_md5: "d".repeat(32),
  control_path: "/c",
  status_path: "/s",
  descriptor_windows_path: "Z:\\d",
};

function envelope(nextGeneration: number, results: any[] = []) {
  return {
    prepared,
    status: {
      session_id: "session",
      app_id: 10,
      ce_sha256: "a".repeat(64),
      table_sha256: "b".repeat(64),
      descriptor_sha256: "c".repeat(64),
      heartbeat_ms: Date.now(),
      attached: true,
      target_process: "game.exe",
      opened_process_id: 42,
      results,
      processes: [],
    },
    next_generation: nextGeneration,
    status_age_ms: 0,
    status_fresh: true,
    status_clock_skew: false,
    session_current: true,
    session_stale_reason: null,
    connected: true,
  };
}

describe("runtime ACK client", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });


  it("validates exact runtime identity even when a query has no MemoryRecords", async () => {
    const gone = { ...envelope(5), connected: false, status_fresh: false, terminal_reason: "owned_bridge_process_exited" };
    api.getRuntimeStatus.mockResolvedValueOnce(gone);

    await expect(queryRuntimeControls(10, [])).rejects.toThrow(/Cheat Engine has exited/);
    expect(api.writeRuntimeCommands).not.toHaveBeenCalled();
  });

  it("says which of the four things a disconnected bridge is", async () => {
    // One sentence covered all of them, and the four need different answers
    // from the reader: start the game, repair the session, start Cheat Engine
    // again, or simply press again.
    const cases: Array<[Record<string, unknown>, RegExp]> = [
      [{ prepared: null, status: null, session_current: false, session_stale_reason: null }, /No Cheat Engine session is prepared/],
      [{ status_unreadable: true, session_state_reason: "control log is not parseable" }, /control log is not parseable/],
      [{ terminal_reason: "owned_bridge_process_exited" }, /Cheat Engine has exited/],
      [{ status_fresh: false, status_age_ms: 9000 }, /has not answered for 9s/],
      [{ status_clock_skew: true }, /dated in the future/],
    ];
    for (const [patch, expected] of cases) {
      api.getRuntimeStatus.mockReset();
      api.getRuntimeStatus.mockResolvedValue({ ...envelope(5), connected: false, ...patch });
      await expect(queryRuntimeControls(10, [])).rejects.toThrow(expected);
    }
    expect(api.writeRuntimeCommands).not.toHaveBeenCalled();
  });

  it("rejects zero-record query when the expected exact session changed", async () => {
    const before = envelope(5);
    const changed = envelope(6);
    changed.prepared = { ...changed.prepared, session_id: "new-session" };
    changed.status = { ...changed.status, session_id: "new-session" };
    api.getRuntimeStatus.mockResolvedValueOnce(changed);

    await expect(queryRuntimeControls(10, [], before)).rejects.toThrow(/session changed/);
    expect(api.writeRuntimeCommands).not.toHaveBeenCalled();
  });

  it("validates the bridge before an empty Apply or bulk-deactivate no-op", async () => {
    const live = envelope(5);
    api.getRuntimeStatus.mockResolvedValueOnce(live).mockResolvedValueOnce(live);

    await expect(applyRuntimeSelection(10, [])).resolves.toEqual({ envelope: live, results: [] });
    await expect(deactivateAllActiveControls(10, [])).resolves.toEqual({ queried: 0, active: 0, deactivated: 0, deactivatedIds: [], corrected: 0, correctedIds: [], envelope: live });
    expect(api.writeRuntimeCommands).not.toHaveBeenCalled();
  });

  it("preserves the current envelope when the bridge is already disconnected", async () => {
    const disconnected = { ...envelope(5), connected: false, status_fresh: false, terminal_reason: "owned_bridge_process_exited" };
    api.getRuntimeStatus.mockResolvedValueOnce(disconnected);

    try {
      await sendRuntimeCommandAndWait(10, { kind: "query", record_id: 7 });
      throw new Error("expected disconnected runtime failure");
    } catch (cause) {
      expect(cause).toBeInstanceOf(RuntimeOperationError);
      expect((cause as RuntimeOperationError).message).toContain("Cheat Engine has exited");
      expect((cause as RuntimeOperationError).envelope).toBe(disconnected);
    }
    expect(api.writeRuntimeCommands).not.toHaveBeenCalled();
  });

  it("waits out a bridge that is running the table's own script before refusing a press", async () => {
    // Cheat Engine writes its heartbeat from a timer on the thread that runs a
    // table's Auto Assembler, so a script that takes longer than the heartbeat
    // to assemble stops it for exactly as long as it runs. That used to refuse
    // the very press that had asked for the script.
    const busy = { ...envelope(5), connected: false, status_fresh: false, status_age_ms: 4000 };
    const live = envelope(5, [{ generation: 5, record_id: 7, ok: true, active: true, value: "1", error: null }]);
    api.getRuntimeStatus.mockResolvedValueOnce(busy).mockResolvedValue(live);
    api.writeRuntimeCommands.mockResolvedValue({ ok: true, count: 1, next_generation: 6 });

    const { result } = await sendRuntimeCommandAndWait(10, { kind: "query", record_id: 7 });
    expect(result.value).toBe("1");
    // It waited rather than writing into a session it could not see.
    expect(api.writeRuntimeCommands).toHaveBeenCalledTimes(1);
  });

  it("bounds the wait for a busy bridge on a clock that cannot be corrected", async () => {
    // `Date.now()` is the wall clock and it is corrected: NTP, a manual change,
    // a device that booted with a bad clock. A twelve second wait computed from
    // it becomes five minutes when the clock moves five minutes back, and the
    // press holding the panel's busy latch is inside that wait, so every other
    // press is refused with "Another CE Decky operation is still running" for
    // as long as it lasts. The other direction ends the wait at once and
    // reports a bridge that was working as gone.
    const busy = { ...envelope(5), connected: false, status_fresh: false, status_age_ms: 4000 };
    api.getRuntimeStatus.mockResolvedValue(busy);
    const wall = vi.spyOn(Date, "now");
    let monotonic = 0;
    const performanceNow = vi.spyOn(performance, "now").mockImplementation(() => monotonic);
    const sleeps: number[] = [];
    const timeout = vi.spyOn(window, "setTimeout").mockImplementation(((handler: any, ms?: number) => {
      sleeps.push(ms ?? 0);
      monotonic += ms ?? 0;
      // The wall clock lurches while the wait is running, in both directions.
      wall.mockReturnValue(sleeps.length % 2 === 0 ? 1_000_000 : 1_000_000 - 5 * 60_000);
      // A wait that is measured on that clock does not end at all while it
      // keeps moving backwards, so this ends it instead of letting the case run
      // until something kills the worker: what the assertions below then read
      // is how far past its bound it went.
      if (sleeps.length >= 200) api.getRuntimeStatus.mockResolvedValue(envelope(5));
      handler();
      return 0 as unknown as ReturnType<typeof window.setTimeout>;
    }) as typeof window.setTimeout);

    try {
      const envelopeRead = await sendRuntimeCommandAndWait(10, { kind: "query", record_id: 7 })
        .then(() => "resolved").catch(() => "refused");

      expect(envelopeRead).toBe("refused");
      // Twelve seconds of polling at 250 ms, and neither lurch shortened or
      // lengthened it: the count is what the elapsed clock allows.
      expect(sleeps.length).toBeGreaterThanOrEqual(47);
      expect(sleeps.length).toBeLessThanOrEqual(49);
      expect(monotonic).toBeGreaterThanOrEqual(12_000);
      expect(monotonic).toBeLessThan(12_500);
    } finally {
      timeout.mockRestore();
      performanceNow.mockRestore();
      wall.mockRestore();
    }
  });

  it("does not abandon an accepted batch because the bridge went quiet running it", async () => {
    const live = envelope(5);
    const busy = { ...envelope(5), connected: false, status_fresh: false, status_age_ms: 5000 };
    const answered = envelope(6, [{ generation: 5, record_id: 7, ok: true, active: true, value: "1", error: null }]);
    api.getRuntimeStatus
      .mockResolvedValueOnce(live)
      .mockResolvedValueOnce(busy)
      .mockResolvedValue(answered);
    api.writeRuntimeCommands.mockResolvedValue({ ok: true, count: 1, next_generation: 6 });

    const { result } = await sendRuntimeCommandAndWait(10, { kind: "set_value", record_id: 7, value: "1" });
    expect(result.ok).toBe(true);
  });

  it("still abandons an accepted batch when the Cheat Engine that owned it is proven gone", async () => {
    const live = envelope(5);
    const exited = { ...envelope(5), connected: false, status_fresh: false, terminal_reason: "owned_bridge_process_exited" };
    api.getRuntimeStatus.mockResolvedValueOnce(live).mockResolvedValue(exited);
    api.writeRuntimeCommands.mockResolvedValue({ ok: true, count: 1, next_generation: 6 });

    await expect(sendRuntimeCommandAndWait(10, { kind: "set_value", record_id: 7, value: "1" }))
      .rejects.toThrow(/Cheat Engine has exited/);
  });

  it("normalizes an initial runtime-status RPC failure into a runtime operation error", async () => {
    api.getRuntimeStatus.mockRejectedValueOnce(new Error("runtime status unavailable"));

    try {
      await sendRuntimeCommandAndWait(10, { kind: "query", record_id: 7 });
      throw new Error("expected runtime status failure");
    } catch (cause) {
      expect(cause).toBeInstanceOf(RuntimeOperationError);
      expect((cause as RuntimeOperationError).message).toContain("runtime status unavailable");
      expect((cause as RuntimeOperationError).envelope).toBeNull();
    }
    expect(api.writeRuntimeCommands).not.toHaveBeenCalled();
  });

  it("rejects a bridge status bound to a different descriptor SHA", async () => {
    const mismatched = envelope(5);
    mismatched.status.descriptor_sha256 = "e".repeat(64);
    api.getRuntimeStatus.mockResolvedValueOnce(mismatched);

    await expect(sendRuntimeCommandAndWait(10, { kind: "query", record_id: 7 })).rejects.toThrow(/identity/);
    expect(api.writeRuntimeCommands).not.toHaveBeenCalled();
  });

  it("does not report command success until the exact generation is acknowledged", async () => {
    api.getRuntimeStatus
      .mockResolvedValueOnce(envelope(5))
      .mockResolvedValueOnce(envelope(6))
      .mockResolvedValueOnce(envelope(6, [{ generation: 5, record_id: 7, ok: true, active: true, value: null, error: null }]));
    api.writeRuntimeCommands.mockResolvedValue({ ok: true, count: 1, next_generation: 6 });
    const result = await sendRuntimeCommandAndWait(10, { kind: "set_active", record_id: 7, value: "1" });
    expect(api.writeRuntimeCommands).toHaveBeenCalledWith(10, [{ generation: 5, kind: "set_active", record_id: 7, value: "1" }]);
    expect(result.result.generation).toBe(5);
    expect(result.result.active).toBe(true);
  });


  it("rejects a negative exact ACK while preserving the latest runtime envelope", async () => {
    const failed = { generation: 5, record_id: 7, ok: false, active: false, value: null, error: "activation failed" };
    api.getRuntimeStatus
      .mockResolvedValueOnce(envelope(5))
      .mockResolvedValueOnce(envelope(6, [failed]));
    api.writeRuntimeCommands.mockResolvedValue({ ok: true, count: 1, next_generation: 6 });

    try {
      await sendRuntimeCommandAndWait(10, { kind: "set_active", record_id: 7, value: "1" });
      throw new Error("expected runtime command failure");
    } catch (cause) {
      expect(cause).toBeInstanceOf(RuntimeOperationError);
      expect((cause as RuntimeOperationError).message).toContain("activation failed");
      expect((cause as RuntimeOperationError).envelope?.status?.results).toEqual([failed]);
    }
  });

  it("preserves the last known envelope when ACK polling itself fails", async () => {
    const before = envelope(5);
    api.getRuntimeStatus
      .mockResolvedValueOnce(before)
      .mockRejectedValueOnce(new Error("runtime status RPC failed"));
    api.writeRuntimeCommands.mockResolvedValue({ ok: true, count: 1, next_generation: 6 });

    try {
      await sendRuntimeCommandAndWait(10, { kind: "query", record_id: 7 });
      throw new Error("expected polling failure");
    } catch (cause) {
      expect(cause).toBeInstanceOf(RuntimeOperationError);
      expect((cause as RuntimeOperationError).message).toContain("runtime status RPC failed");
      expect((cause as RuntimeOperationError).envelope).toBe(before);
    }
  });

  it("rejects an ACK whose generation belongs to a different MemoryRecord", async () => {
    api.getRuntimeStatus
      .mockResolvedValueOnce(envelope(5))
      .mockResolvedValueOnce(envelope(6, [{ generation: 5, record_id: 8, ok: true, active: true, value: null, error: null }]));
    api.writeRuntimeCommands.mockResolvedValue({ ok: true, count: 1, next_generation: 6 });

    await expect(sendRuntimeCommandAndWait(10, { kind: "query", record_id: 7 })).rejects.toThrow(/identity mismatch/);
  });

  it("rejects descriptor identity drift before accepting an ACK", async () => {
    const drifted = envelope(6, [{ generation: 5, record_id: 7, ok: true, active: true, value: null, error: null }]);
    drifted.status!.descriptor_sha256 = "e".repeat(64);
    api.getRuntimeStatus
      .mockResolvedValueOnce(envelope(5))
      .mockResolvedValueOnce(drifted);
    api.writeRuntimeCommands.mockResolvedValue({ ok: true, count: 1, next_generation: 6 });

    await expect(sendRuntimeCommandAndWait(10, { kind: "query", record_id: 7 })).rejects.toThrow(/identity/);
  });

  it("preserves the pre-write envelope when the backend rejects a runtime write", async () => {
    api.getRuntimeStatus.mockResolvedValueOnce(envelope(5));
    api.writeRuntimeCommands.mockRejectedValueOnce(new Error("target detached"));

    try {
      await sendRuntimeCommandAndWait(10, { kind: "query", record_id: 7 });
      throw new Error("expected backend write failure");
    } catch (cause) {
      expect(cause).toBeInstanceOf(RuntimeOperationError);
      expect((cause as RuntimeOperationError).message).toContain("target detached");
      expect((cause as RuntimeOperationError).envelope?.prepared?.session_id).toBe("session");
    }
  });

  it("queries, deactivates, and re-queries only controls reported active", async () => {
    const queryResults = [
      { generation: 1, record_id: 1, ok: true, active: true, value: null, error: null },
      { generation: 2, record_id: 2, ok: true, active: false, value: null, error: null },
      { generation: 3, record_id: 3, ok: true, active: true, value: null, error: null },
    ];
    const deactivateResults = [
      { generation: 4, record_id: 1, ok: true, active: false, value: null, error: null },
      { generation: 5, record_id: 3, ok: true, active: false, value: null, error: null },
    ];
    const verifyResults = [
      { generation: 6, record_id: 1, ok: true, active: false, value: null, error: null },
      { generation: 7, record_id: 3, ok: true, active: false, value: null, error: null },
    ];

    api.getRuntimeStatus
      .mockResolvedValueOnce(envelope(1))
      .mockResolvedValueOnce(envelope(4, queryResults))
      .mockResolvedValueOnce(envelope(4, queryResults))
      .mockResolvedValueOnce(envelope(6, [...queryResults, ...deactivateResults]))
      .mockResolvedValueOnce(envelope(6, [...queryResults, ...deactivateResults]))
      .mockResolvedValueOnce(envelope(8, [...queryResults, ...deactivateResults, ...verifyResults]));
    api.writeRuntimeCommands
      .mockResolvedValueOnce({ ok: true, count: 3, next_generation: 4 })
      .mockResolvedValueOnce({ ok: true, count: 2, next_generation: 6 })
      .mockResolvedValueOnce({ ok: true, count: 2, next_generation: 8 });

    const result = await deactivateAllActiveControls(10, [1, 2, 3]);
    // The exact set matters: the caller remembers only what it switched off.
    expect(result).toMatchObject({ queried: 3, active: 2, deactivated: 2, deactivatedIds: [1, 3] });
    expect(api.writeRuntimeCommands.mock.calls[1][1].map((command: any) => command.record_id)).toEqual([1, 3]);
    expect(api.writeRuntimeCommands.mock.calls[2][1].every((command: any) => command.kind === "query")).toBe(true);
  });
  it("puts every switch back to its off key after the release", async () => {
    // Releasing the freeze leaves the record holding the value it was frozen
    // at, so a table of flags would report nothing active with every flag in
    // the game still set. The write comes after the verification, so what the
    // game keeps is what was written last.
    const queryResults = [
      { generation: 1, record_id: 1, ok: true, active: true, value: "1", error: null },
      { generation: 2, record_id: 2, ok: true, active: true, value: "7", error: null },
    ];
    const deactivateResults = [
      { generation: 3, record_id: 1, ok: true, active: false, value: "1", error: null },
      { generation: 4, record_id: 2, ok: true, active: false, value: "7", error: null },
    ];
    const verifyResults = [
      { generation: 5, record_id: 1, ok: true, active: false, value: "1", error: null },
      { generation: 6, record_id: 2, ok: true, active: false, value: "7", error: null },
    ];
    const restored = [{ generation: 7, record_id: 1, ok: true, active: false, value: "0", error: null }];
    api.getRuntimeStatus
      .mockResolvedValueOnce(envelope(1))
      .mockResolvedValueOnce(envelope(3, queryResults))
      .mockResolvedValueOnce(envelope(3, queryResults))
      .mockResolvedValueOnce(envelope(5, [...queryResults, ...deactivateResults]))
      .mockResolvedValueOnce(envelope(5, [...queryResults, ...deactivateResults]))
      .mockResolvedValueOnce(envelope(7, [...queryResults, ...deactivateResults, ...verifyResults]))
      .mockResolvedValueOnce(envelope(7, [...queryResults, ...deactivateResults, ...verifyResults]))
      .mockResolvedValueOnce(envelope(8, [...queryResults, ...deactivateResults, ...verifyResults, ...restored]));
    api.writeRuntimeCommands
      .mockResolvedValueOnce({ ok: true, count: 2, next_generation: 3 })
      .mockResolvedValueOnce({ ok: true, count: 2, next_generation: 5 })
      .mockResolvedValueOnce({ ok: true, count: 2, next_generation: 7 })
      .mockResolvedValueOnce({ ok: true, count: 1, next_generation: 8 });

    // Only record 1 is a switch; record 2 keeps whatever it holds.
    await deactivateAllActiveControls(10, [1, 2], new Map([[1, "0"]]));
    const last = api.writeRuntimeCommands.mock.calls.at(-1)![1];
    expect(last.map((command: any) => [command.kind, command.record_id, command.value])).toEqual([
      ["set_value", 1, "0"],
    ]);
  });

  it("reports partial bulk deactivation with the latest bridge state", async () => {
    const queryResults = [
      { generation: 1, record_id: 1, ok: true, active: true, value: null, error: null },
    ];
    const failedDeactivate = [
      { generation: 2, record_id: 1, ok: false, active: true, value: null, error: "write rejected" },
    ];
    api.getRuntimeStatus
      .mockResolvedValueOnce(envelope(1))
      .mockResolvedValueOnce(envelope(2, queryResults))
      .mockResolvedValueOnce(envelope(2, queryResults))
      .mockResolvedValueOnce(envelope(3, [...queryResults, ...failedDeactivate]));
    api.writeRuntimeCommands
      .mockResolvedValueOnce({ ok: true, count: 1, next_generation: 2 })
      .mockResolvedValueOnce({ ok: true, count: 1, next_generation: 3 });

    try {
      await deactivateAllActiveControls(10, [1]);
      throw new Error("expected bulk deactivation failure");
    } catch (cause) {
      expect(cause).toBeInstanceOf(RuntimeOperationError);
      expect((cause as RuntimeOperationError).message).toContain("Bulk deactivation incomplete");
      expect((cause as RuntimeOperationError).envelope?.status?.results.at(-1)).toEqual(failedDeactivate[0]);
    }
  });

  it("stops bulk deactivation before mutation when a query omits active state", async () => {
    const unknown = [{ generation: 1, record_id: 1, ok: true, active: null, value: "1", error: null }];
    api.getRuntimeStatus
      .mockResolvedValueOnce(envelope(1))
      .mockResolvedValueOnce(envelope(2, unknown));
    api.writeRuntimeCommands.mockResolvedValueOnce({ ok: true, count: 1, next_generation: 2 });

    await expect(deactivateAllActiveControls(10, [1])).rejects.toThrow(/did not return an active state/);
    expect(api.writeRuntimeCommands).toHaveBeenCalledTimes(1);
  });

  it("stops sending later deactivation chunks after the first failed chunk", async () => {
    const ids = Array.from({ length: 65 }, (_, index) => index + 1);
    const firstQuery = ids.slice(0, 64).map((recordId, index) => ({
      generation: index + 1, record_id: recordId, ok: true, active: true, value: null, error: null,
    }));
    const secondQuery = { generation: 65, record_id: 65, ok: true, active: true, value: null, error: null };
    const firstDeactivate = ids.slice(0, 64).map((recordId, index) => ({
      generation: 66 + index,
      record_id: recordId,
      ok: index !== 4,
      active: index === 4 ? true : false,
      value: null,
      error: index === 4 ? "write rejected" : null,
    }));
    api.getRuntimeStatus
      .mockResolvedValueOnce(envelope(1))
      .mockResolvedValueOnce(envelope(65, firstQuery))
      .mockResolvedValueOnce(envelope(65, firstQuery))
      .mockResolvedValueOnce(envelope(66, [...firstQuery, secondQuery]))
      .mockResolvedValueOnce(envelope(66, [...firstQuery, secondQuery]))
      .mockResolvedValueOnce(envelope(130, [...firstQuery, secondQuery, ...firstDeactivate]));
    api.writeRuntimeCommands
      .mockResolvedValueOnce({ ok: true, count: 64, next_generation: 65 })
      .mockResolvedValueOnce({ ok: true, count: 1, next_generation: 66 })
      .mockResolvedValueOnce({ ok: true, count: 64, next_generation: 130 });

    await expect(deactivateAllActiveControls(10, ids)).rejects.toThrow(/No later batch was sent/);
    expect(api.writeRuntimeCommands).toHaveBeenCalledTimes(3);
    expect(api.writeRuntimeCommands.mock.calls[2][1]).toHaveLength(64);
  });

  it("reports already-deactivated progress when a later deactivation chunk loses runtime status", async () => {
    const ids = Array.from({ length: 65 }, (_, index) => index + 1);
    const firstQuery = ids.slice(0, 64).map((recordId, index) => ({
      generation: index + 1, record_id: recordId, ok: true, active: true, value: null, error: null,
    }));
    const secondQuery = { generation: 65, record_id: 65, ok: true, active: true, value: null, error: null };
    const firstDeactivate = ids.slice(0, 64).map((recordId, index) => ({
      generation: 66 + index, record_id: recordId, ok: true, active: false, value: null, error: null,
    }));
    const afterFirstDeactivate = envelope(130, [...firstQuery, secondQuery, ...firstDeactivate]);
    api.getRuntimeStatus
      .mockResolvedValueOnce(envelope(1))
      .mockResolvedValueOnce(envelope(65, firstQuery))
      .mockResolvedValueOnce(envelope(65, firstQuery))
      .mockResolvedValueOnce(envelope(66, [...firstQuery, secondQuery]))
      .mockResolvedValueOnce(envelope(66, [...firstQuery, secondQuery]))
      .mockResolvedValueOnce(afterFirstDeactivate)
      .mockRejectedValueOnce(new Error("runtime status RPC failed"));
    api.writeRuntimeCommands
      .mockResolvedValueOnce({ ok: true, count: 64, next_generation: 65 })
      .mockResolvedValueOnce({ ok: true, count: 1, next_generation: 66 })
      .mockResolvedValueOnce({ ok: true, count: 64, next_generation: 130 });

    try {
      await deactivateAllActiveControls(10, ids);
      throw new Error("expected later-chunk interruption");
    } catch (cause) {
      expect(cause).toBeInstanceOf(RuntimeOperationError);
      expect((cause as RuntimeOperationError).message).toContain("interrupted after 64/65");
      expect((cause as RuntimeOperationError).envelope).toBe(afterFirstDeactivate);
    }
    expect(api.writeRuntimeCommands).toHaveBeenCalledTimes(3);
  });


  it("fails closed before bulk mutation when the exact session changes after the query phase", async () => {
    const queryResult = { generation: 1, record_id: 1, ok: true, active: true, value: null, error: null };
    const queriedSession = envelope(2, [queryResult]);
    const replacement = envelope(2, [queryResult]);
    replacement.prepared = { ...prepared, session_id: "replacement-session" };
    replacement.status = { ...replacement.status!, session_id: "replacement-session" };

    api.getRuntimeStatus
      .mockResolvedValueOnce(envelope(1))
      .mockResolvedValueOnce(queriedSession)
      .mockResolvedValueOnce(replacement);
    api.writeRuntimeCommands
      .mockResolvedValueOnce({ ok: true, count: 1, next_generation: 2 });

    await expect(deactivateAllActiveControls(10, [1])).rejects.toThrow(/session changed/);
    expect(api.writeRuntimeCommands).toHaveBeenCalledTimes(1);
  });

  it("fails closed when a multi-chunk query crosses into a different exact runtime session", async () => {
    const ids = Array.from({ length: 65 }, (_, index) => index + 1);
    const firstResults = ids.slice(0, 64).map((recordId, index) => ({
      generation: index + 1, record_id: recordId, ok: true, active: false, value: null, error: null,
    }));
    const firstSession = envelope(1);
    const firstAck = envelope(65, firstResults);
    const secondBefore = envelope(65, firstResults);
    const secondSession = envelope(66, [
      ...firstResults,
      { generation: 65, record_id: 65, ok: true, active: false, value: null, error: null },
    ]);
    secondBefore.prepared = { ...prepared, session_id: "replacement-session" };
    secondBefore.status = { ...secondBefore.status!, session_id: "replacement-session" };
    secondSession.prepared = { ...prepared, session_id: "replacement-session" };
    secondSession.status = { ...secondSession.status!, session_id: "replacement-session" };

    api.getRuntimeStatus
      .mockResolvedValueOnce(firstSession)
      .mockResolvedValueOnce(firstAck)
      .mockResolvedValueOnce(secondBefore)
      .mockResolvedValueOnce(secondSession);
    api.writeRuntimeCommands
      .mockResolvedValueOnce({ ok: true, count: 64, next_generation: 65 })
      .mockResolvedValueOnce({ ok: true, count: 1, next_generation: 66 });

    await expect(queryRuntimeControls(10, ids)).rejects.toThrow(/session changed/);
  });

  it("applies only the runtime diff and verifies the requested state before returning", async () => {
    const initialQuery = [{ generation: 1, record_id: 7, ok: true, active: false, value: "1", error: null }];
    const mutation = [
      { generation: 2, record_id: 7, ok: true, active: false, value: "2", error: null },
      { generation: 3, record_id: 7, ok: true, active: true, value: "2", error: null },
    ];
    const verified = [{ generation: 4, record_id: 7, ok: true, active: true, value: "2", error: null }];
    api.getRuntimeStatus
      .mockResolvedValueOnce(envelope(1))
      .mockResolvedValueOnce(envelope(2, initialQuery))
      .mockResolvedValueOnce(envelope(2, initialQuery))
      .mockResolvedValueOnce(envelope(4, [...initialQuery, ...mutation]))
      .mockResolvedValueOnce(envelope(4, [...initialQuery, ...mutation]))
      .mockResolvedValueOnce(envelope(5, [...initialQuery, ...mutation, ...verified]));
    api.writeRuntimeCommands
      .mockResolvedValueOnce({ ok: true, count: 1, next_generation: 2 })
      .mockResolvedValueOnce({ ok: true, count: 2, next_generation: 4 })
      .mockResolvedValueOnce({ ok: true, count: 1, next_generation: 5 });

    const result = await applyRuntimeSelection(10, [{ record_id: 7, active: true, value: "2" }]);
    expect(api.writeRuntimeCommands.mock.calls[1][1].map((command: any) => command.kind)).toEqual(["set_value", "set_active"]);
    expect(result.results.at(-1)).toMatchObject({ record_id: 7, active: true, value: "2" });
  });


  it("switches a switch record off by writing the other key, after releasing it", async () => {
    // Releasing the freeze is not switching such a cheat off: the record keeps
    // the value it was frozen at, so a `0:Disabled/1:Enabled` flag would report
    // itself off with the cheat still running in the game.
    const initialQuery = [{ generation: 1, record_id: 7, ok: true, active: true, value: "1", error: null }];
    const mutation = [
      { generation: 2, record_id: 7, ok: true, active: false, value: "1", error: null },
      { generation: 3, record_id: 7, ok: true, active: false, value: "0", error: null },
    ];
    const verified = [{ generation: 4, record_id: 7, ok: true, active: false, value: "0", error: null }];
    api.getRuntimeStatus
      .mockResolvedValueOnce(envelope(1))
      .mockResolvedValueOnce(envelope(2, initialQuery))
      .mockResolvedValueOnce(envelope(2, initialQuery))
      .mockResolvedValueOnce(envelope(4, [...initialQuery, ...mutation]))
      .mockResolvedValueOnce(envelope(4, [...initialQuery, ...mutation]))
      .mockResolvedValueOnce(envelope(5, [...initialQuery, ...mutation, ...verified]));
    api.writeRuntimeCommands
      .mockResolvedValueOnce({ ok: true, count: 1, next_generation: 2 })
      .mockResolvedValueOnce({ ok: true, count: 2, next_generation: 4 })
      .mockResolvedValueOnce({ ok: true, count: 1, next_generation: 5 });

    await applyRuntimeSelection(10, [
      { record_id: 7, active: false, value: "1", switch_values: { on: "1", off: "0" } },
    ]);
    const sent = api.writeRuntimeCommands.mock.calls[1][1];
    // The release first, so a refusal leaves nothing written, and the key the
    // author gave the off side rather than a literal zero.
    expect(sent.map((command: any) => [command.kind, command.value])).toEqual([
      ["set_active", "0"], ["set_value", "0"],
    ]);
  });

  it("switches a switch record on by writing its own on key first", async () => {
    // The key is whatever the author wrote: the corpus carries ('1','2') pairs
    // and records whose active side is 0, so a literal 1 would switch those the
    // wrong way. The staged `value` is ignored; the toggle is the value.
    const initialQuery = [{ generation: 1, record_id: 7, ok: true, active: false, value: "1", error: null }];
    const mutation = [
      { generation: 2, record_id: 7, ok: true, active: false, value: "2", error: null },
      { generation: 3, record_id: 7, ok: true, active: true, value: "2", error: null },
    ];
    const verified = [{ generation: 4, record_id: 7, ok: true, active: true, value: "2", error: null }];
    api.getRuntimeStatus
      .mockResolvedValueOnce(envelope(1))
      .mockResolvedValueOnce(envelope(2, initialQuery))
      .mockResolvedValueOnce(envelope(2, initialQuery))
      .mockResolvedValueOnce(envelope(4, [...initialQuery, ...mutation]))
      .mockResolvedValueOnce(envelope(4, [...initialQuery, ...mutation]))
      .mockResolvedValueOnce(envelope(5, [...initialQuery, ...mutation, ...verified]));
    api.writeRuntimeCommands
      .mockResolvedValueOnce({ ok: true, count: 1, next_generation: 2 })
      .mockResolvedValueOnce({ ok: true, count: 2, next_generation: 4 })
      .mockResolvedValueOnce({ ok: true, count: 1, next_generation: 5 });

    await applyRuntimeSelection(10, [
      { record_id: 7, active: true, value: "ignored", switch_values: { on: "2", off: "1" } },
    ]);
    expect(api.writeRuntimeCommands.mock.calls[1][1].map((command: any) => [command.kind, command.value])).toEqual([
      ["set_value", "2"], ["set_active", "1"],
    ]);
  });

  it("records the newly activated cheat even when an earlier selection was already active", async () => {
    const queried = [
      { generation: 1, record_id: 1, ok: true, active: true, value: null, error: null },
      { generation: 2, record_id: 7, ok: true, active: false, value: null, error: null },
    ];
    const mutated = [{ generation: 3, record_id: 7, ok: true, active: true, value: null, error: null }];
    const verified = [
      { ...queried[0], generation: 4 },
      { ...mutated[0], generation: 5 },
    ];
    api.getRuntimeStatus
      .mockResolvedValueOnce(envelope(1)).mockResolvedValueOnce(envelope(3, queried))
      .mockResolvedValueOnce(envelope(3, queried)).mockResolvedValueOnce(envelope(4, [...queried, ...mutated]))
      .mockResolvedValueOnce(envelope(4, [...queried, ...mutated])).mockResolvedValueOnce(envelope(6, verified));
    api.writeRuntimeCommands
      .mockResolvedValueOnce({ ok: true, count: 2, next_generation: 3 })
      .mockResolvedValueOnce({ ok: true, count: 1, next_generation: 4 })
      .mockResolvedValueOnce({ ok: true, count: 2, next_generation: 6 });
    const applied = await applyRuntimeSelection(10, [
      { record_id: 1, active: true, value: null }, { record_id: 7, active: true, value: null },
    ]);
    expect(applied.compatibilityConfirmed).toBe(true);
    expect(applied.compatibilityMayHaveChanged).toBe(true);
    expect(api.confirmTableWorking).toHaveBeenCalledWith(10, prepared.table_sha256, prepared.session_id, 7);
  });

  it("reports that compatibility may have changed when the confirmation itself is uncertain", async () => {
    // A durability the backend cannot prove is reported as a failure while the
    // row it wrote is already visible, so the caller still has to reread it.
    api.confirmTableWorking.mockRejectedValueOnce(new Error("compatibility durability is unknown"));
    const queried = [
      { generation: 1, record_id: 1, ok: true, active: true, value: null, error: null },
      { generation: 2, record_id: 7, ok: true, active: false, value: null, error: null },
    ];
    const mutated = [{ generation: 3, record_id: 7, ok: true, active: true, value: null, error: null }];
    const verified = [
      { ...queried[0], generation: 4 },
      { ...mutated[0], generation: 5 },
    ];
    api.getRuntimeStatus
      .mockResolvedValueOnce(envelope(1)).mockResolvedValueOnce(envelope(3, queried))
      .mockResolvedValueOnce(envelope(3, queried)).mockResolvedValueOnce(envelope(4, [...queried, ...mutated]))
      .mockResolvedValueOnce(envelope(4, [...queried, ...mutated])).mockResolvedValueOnce(envelope(6, verified));
    api.writeRuntimeCommands
      .mockResolvedValueOnce({ ok: true, count: 2, next_generation: 3 })
      .mockResolvedValueOnce({ ok: true, count: 1, next_generation: 4 })
      .mockResolvedValueOnce({ ok: true, count: 2, next_generation: 6 });
    const applied = await applyRuntimeSelection(10, [
      { record_id: 1, active: true, value: null }, { record_id: 7, active: true, value: null },
    ]);
    expect(applied.compatibilityConfirmed).toBe(false);
    expect(applied.compatibilityMayHaveChanged).toBe(true);
  });

  it("orders nested live mutations parent-before-child instead of by numeric record ID", async () => {
    const queried = [
      { generation: 1, record_id: 20, ok: true, active: false, value: null, error: null },
      { generation: 2, record_id: 10, ok: true, active: false, value: null, error: null },
    ];
    const mutated = [
      { generation: 3, record_id: 20, ok: true, active: true, value: null, error: null },
      { generation: 4, record_id: 10, ok: true, active: true, value: null, error: null },
    ];
    const verified = [
      { generation: 5, record_id: 20, ok: true, active: true, value: null, error: null },
      { generation: 6, record_id: 10, ok: true, active: true, value: null, error: null },
    ];
    api.getRuntimeStatus
      .mockResolvedValueOnce(envelope(1))
      .mockResolvedValueOnce(envelope(3, queried))
      .mockResolvedValueOnce(envelope(3, queried))
      .mockResolvedValueOnce(envelope(5, [...queried, ...mutated]))
      .mockResolvedValueOnce(envelope(5, [...queried, ...mutated]))
      .mockResolvedValueOnce(envelope(7, [...queried, ...mutated, ...verified]));
    api.writeRuntimeCommands
      .mockResolvedValueOnce({ ok: true, count: 2, next_generation: 3 })
      .mockResolvedValueOnce({ ok: true, count: 2, next_generation: 5 })
      .mockResolvedValueOnce({ ok: true, count: 2, next_generation: 7 });

    await applyRuntimeSelection(10, [
      { record_id: 10, active: true, value: null, structure: ["Parent", "Child"] },
      { record_id: 20, active: true, value: null, structure: ["Parent"] },
    ]);

    expect(api.writeRuntimeCommands.mock.calls[1][1].map((command: any) => command.record_id)).toEqual([20, 10]);
  });

  it("explains a script Cheat Engine ran and refused, instead of repeating the bridge's own words", async () => {
    // A cheat table finds the game's code by scanning for byte patterns, so a
    // table written against an older build of the game fails exactly here.
    // "activation did not settle" tells nobody that.
    const queried = [{ generation: 1, record_id: 7, ok: true, active: false, value: null, error: null }];
    const refused = [{
      generation: 2, record_id: 7, ok: false, active: false, value: null,
      error: "activation did not settle", error_code: "activation_rejected",
    }];
    // Read back once more, because only then is it known that nothing else in
    // the call came on, which is what makes the refusal the table's.
    const readBack = [{ generation: 3, record_id: 7, ok: true, active: false, value: null, error: null }];
    api.getRuntimeStatus
      .mockResolvedValueOnce(envelope(1))
      .mockResolvedValueOnce(envelope(2, queried))
      .mockResolvedValueOnce(envelope(2, queried))
      .mockResolvedValueOnce(envelope(3, [...queried, ...refused]))
      .mockResolvedValueOnce(envelope(3, [...queried, ...refused]))
      .mockResolvedValue(envelope(4, [...queried, ...refused, ...readBack]));
    api.writeRuntimeCommands
      .mockResolvedValueOnce({ ok: true, count: 1, next_generation: 2 })
      .mockResolvedValueOnce({ ok: true, count: 1, next_generation: 3 })
      .mockResolvedValueOnce({ ok: true, count: 1, next_generation: 4 });

    await expect(applyRuntimeSelection(10, [
      { record_id: 7, active: true, value: null, label: "Init -- ENABLE THIS FIRST" },
    ])).rejects.toThrow(/“Init -- ENABLE THIS FIRST” did not switch on.*different build of the game/s);
  });

  it("says the game closed rather than blaming the build, and still asks about the table", async () => {
    // On the device the game ended in the same second its activation came back
    // off, and the panel said the table was written for another build. The
    // cheat may still be what closed it, so the question about the table stays.
    const queried = [{ generation: 1, record_id: 7, ok: true, active: false, value: null, error: null }];
    const closed = [{
      generation: 2, record_id: 7, ok: false, active: false, value: null,
      error: "the game closed while it was being switched on", error_code: "target_exited",
    }];
    api.getRuntimeStatus
      .mockResolvedValueOnce(envelope(1))
      .mockResolvedValueOnce(envelope(2, queried))
      .mockResolvedValueOnce(envelope(2, queried))
      .mockResolvedValueOnce(envelope(3, [...queried, ...closed]))
      .mockResolvedValue(envelope(3, [...queried, ...closed]));
    api.writeRuntimeCommands
      .mockResolvedValueOnce({ ok: true, count: 1, next_generation: 2 })
      .mockResolvedValueOnce({ ok: true, count: 1, next_generation: 3 });

    const failure = await applyRuntimeSelection(10, [
      { record_id: 7, active: true, value: null, label: "Activate" },
    ]).catch((cause) => cause);

    expect(failure).toBeInstanceOf(RuntimeOperationError);
    expect(failure.message).toContain("“Activate” did not finish switching on: the game closed while it was being switched on.");
    expect(failure.message).not.toContain("different build");
    expect(failure.tableRefused).toBe(true);
  });

  it("says whose ask a flag was when the one CE Decky put down would not go", async () => {
    // These records are not the reader's choice: the table's own script
    // declares them on, and the press writes each down so switching on one
    // cheat does not switch on the rest of the table. One that will not go down
    // is worth an error - a cheat is running that nobody asked for - but a
    // reader met it as a bare name they had never seen, from a press where they
    // had switched nothing on.
    // Read as the script left it, written down, and still reading as the script
    // left it: the flag would not go.
    const queried = [{ generation: 1, record_id: 9, ok: true, active: false, value: "1", error: null }];
    const mutation = [{ generation: 2, record_id: 9, ok: true, active: false, value: "1", error: null }];
    const verified = [{ generation: 3, record_id: 9, ok: true, active: false, value: "1", error: null }];
    api.getRuntimeStatus
      .mockResolvedValueOnce(envelope(1))
      .mockResolvedValueOnce(envelope(2, queried))
      .mockResolvedValueOnce(envelope(2, queried))
      .mockResolvedValueOnce(envelope(3, [...queried, ...mutation]))
      .mockResolvedValueOnce(envelope(3, [...queried, ...mutation]))
      .mockResolvedValue(envelope(4, [...queried, ...mutation, ...verified]));
    api.writeRuntimeCommands
      .mockResolvedValueOnce({ ok: true, count: 1, next_generation: 2 })
      .mockResolvedValueOnce({ ok: true, count: 1, next_generation: 3 })
      .mockResolvedValueOnce({ ok: true, count: 1, next_generation: 4 });

    const failure = await applyRuntimeSelection(10, [
      { record_id: 9, active: null, value: "0", label: "bEnableVitalsDrainRateMod", held_off: true },
    ]).catch((cause) => cause);

    expect(failure).toBeInstanceOf(RuntimeOperationError);
    expect(failure.message).toMatch(/switched on by this table's own script/);
    expect(failure.message).toMatch(/you did not ask for it/);
  });

  it("does not call the table unusable when a cheat would not switch off", async () => {
    // The bridge reports one code for a `set_active` that settled wrong, in
    // either direction. A cheat that will not switch *off* is very likely still
    // running in the game, and calling the table unusable there both
    // misdescribes it and hides that.
    const queried = [{ generation: 1, record_id: 7, ok: true, active: true, value: null, error: null }];
    const refused = [{
      generation: 2, record_id: 7, ok: false, active: true, value: null,
      error: "activation did not settle", error_code: "activation_rejected",
    }];
    api.getRuntimeStatus
      .mockResolvedValueOnce(envelope(1))
      .mockResolvedValueOnce(envelope(2, queried))
      .mockResolvedValueOnce(envelope(2, queried))
      .mockResolvedValueOnce(envelope(3, [...queried, ...refused]))
      .mockResolvedValue(envelope(3, [...queried, ...refused]));
    api.writeRuntimeCommands
      .mockResolvedValueOnce({ ok: true, count: 1, next_generation: 2 })
      .mockResolvedValueOnce({ ok: true, count: 1, next_generation: 3 });

    const failure = await applyRuntimeSelection(10, [
      { record_id: 7, active: false, value: null, label: "Invincible" },
    ]).catch((cause) => cause);
    expect(failure).toBeInstanceOf(RuntimeOperationError);
    expect(failure.message).toMatch(/did not switch off/);
    expect(failure.tableRefused).toBe(false);
  });

  it("keeps Cheat Engine's own reason when there is one", async () => {
    const queried = [{ generation: 1, record_id: 7, ok: true, active: false, value: null, error: null }];
    const failed = [{
      generation: 2, record_id: 7, ok: false, active: false, value: null,
      error: "target process is not attached", error_code: "target_detached",
    }];
    api.getRuntimeStatus
      .mockResolvedValueOnce(envelope(1))
      .mockResolvedValueOnce(envelope(2, queried))
      .mockResolvedValueOnce(envelope(2, queried))
      .mockResolvedValueOnce(envelope(3, [...queried, ...failed]))
      .mockResolvedValue(envelope(3, [...queried, ...failed]));
    api.writeRuntimeCommands
      .mockResolvedValueOnce({ ok: true, count: 1, next_generation: 2 })
      .mockResolvedValueOnce({ ok: true, count: 1, next_generation: 3 });

    await expect(applyRuntimeSelection(10, [
      { record_id: 7, active: true, value: null, label: "Init -- ENABLE THIS FIRST" },
    ])).rejects.toThrow("target process is not attached");
  });

  it("rejects duplicate desired MemoryRecord identities before any runtime RPC", async () => {
    await expect(applyRuntimeSelection(10, [
      { record_id: 7, active: true, value: null },
      { record_id: 7, active: false, value: null },
    ])).rejects.toThrow(/duplicate MemoryRecord 7/);
    expect(api.getRuntimeStatus).not.toHaveBeenCalled();
  });

});

describe("nested MemoryRecord topology", () => {
  beforeEach(() => {
    // `clearAllMocks` keeps implementations, so a leftover `mockResolvedValue`
    // from an earlier case would answer this one's generation handshake.
    api.getRuntimeStatus.mockReset();
    api.writeRuntimeCommands.mockReset();
  });

  function query(generation: number, recordId: number, active: boolean | null, ok = true) {
    return {
      generation, record_id: recordId, active, value: null, ok,
      error: ok ? null : "MemoryRecord missing",
      error_code: ok ? null : "record_missing",
    };
  }

  function activeCommands() {
    return api.writeRuntimeCommands.mock.calls
      .flatMap(([, commands]: any) => commands)
      .filter((command: any) => command.kind === "set_active")
      .map((command: any) => command.record_id);
  }

  it("switches a nested cheat off before the script that owns it", async () => {
    // Disabling a parent destroys the records it created, so the deepest record
    // must be switched off while it still exists. Sorting parent-first meant
    // Configure cheats could destroy a descendant it was about to verify.
    const before = [query(1, 2, true), query(2, 1, true)];
    const mutated = [query(3, 2, false), query(4, 1, false)];
    const verified = [query(5, 2, false), query(6, 1, false)];
    api.getRuntimeStatus
      .mockResolvedValueOnce(envelope(1))
      .mockResolvedValueOnce(envelope(3, before))
      .mockResolvedValueOnce(envelope(3, before))
      .mockResolvedValueOnce(envelope(5, [...before, ...mutated]))
      .mockResolvedValueOnce(envelope(5, [...before, ...mutated]))
      .mockResolvedValueOnce(envelope(7, [...before, ...mutated, ...verified]));
    api.writeRuntimeCommands
      .mockResolvedValueOnce({ ok: true, count: 2, next_generation: 3 })
      .mockResolvedValueOnce({ ok: true, count: 2, next_generation: 5 })
      .mockResolvedValueOnce({ ok: true, count: 2, next_generation: 7 });

    await applyRuntimeSelection(10, [
      { record_id: 1, active: false, value: null, structure: ["Script"] },
      { record_id: 2, active: false, value: null, structure: ["Script", "Child"] },
    ]);

    expect(activeCommands()).toEqual([2, 1]);
  });

  it("switches enclosing scripts on before the cheat inside them", async () => {
    const before = [query(1, 1, false), query(2, 2, false)];
    const mutated = [query(3, 1, true), query(4, 2, true)];
    const verified = [query(5, 1, true), query(6, 2, true)];
    api.getRuntimeStatus
      .mockResolvedValueOnce(envelope(1))
      .mockResolvedValueOnce(envelope(3, before))
      .mockResolvedValueOnce(envelope(3, before))
      .mockResolvedValueOnce(envelope(5, [...before, ...mutated]))
      .mockResolvedValueOnce(envelope(5, [...before, ...mutated]))
      .mockResolvedValueOnce(envelope(7, [...before, ...mutated, ...verified]));
    api.writeRuntimeCommands
      .mockResolvedValueOnce({ ok: true, count: 2, next_generation: 3 })
      .mockResolvedValueOnce({ ok: true, count: 2, next_generation: 5 })
      .mockResolvedValueOnce({ ok: true, count: 2, next_generation: 7 });

    await applyRuntimeSelection(10, [
      { record_id: 2, active: true, value: null, structure: ["Script", "Child"] },
      { record_id: 1, active: true, value: null, structure: ["Script"] },
    ]);

    expect(activeCommands()).toEqual([1, 2]);
  });

  it("enables the enclosing script first, then the child it creates", async () => {
    // The real shape: the child does not exist until the parent script runs.
    // A strict preflight over every desired ID aborted before the parent
    // activation was ever sent, so enabling a nested cheat could never work.
    const beforeQuery = [query(1, 1, false), query(2, 2, null, false)];
    const parentOn = [query(3, 1, true)];
    const childNow = [query(4, 2, false)];
    const childOn = [query(5, 2, true)];
    const verified = [query(6, 1, true), query(7, 2, true)];
    api.getRuntimeStatus
      .mockResolvedValueOnce(envelope(1))
      .mockResolvedValueOnce(envelope(3, beforeQuery))
      .mockResolvedValueOnce(envelope(3, beforeQuery))
      .mockResolvedValueOnce(envelope(4, [...beforeQuery, ...parentOn]))
      .mockResolvedValueOnce(envelope(4, [...beforeQuery, ...parentOn]))
      .mockResolvedValueOnce(envelope(5, [...beforeQuery, ...parentOn, ...childNow]))
      .mockResolvedValueOnce(envelope(5, [...beforeQuery, ...parentOn, ...childNow]))
      .mockResolvedValueOnce(envelope(6, [...beforeQuery, ...parentOn, ...childNow, ...childOn]))
      .mockResolvedValueOnce(envelope(6, [...beforeQuery, ...parentOn, ...childNow, ...childOn]))
      .mockResolvedValue(envelope(8, [...beforeQuery, ...parentOn, ...childNow, ...childOn, ...verified]));
    api.writeRuntimeCommands
      .mockResolvedValueOnce({ ok: true, count: 2, next_generation: 3 })
      .mockResolvedValueOnce({ ok: true, count: 1, next_generation: 4 })
      .mockResolvedValueOnce({ ok: true, count: 1, next_generation: 5 })
      .mockResolvedValueOnce({ ok: true, count: 1, next_generation: 6 })
      .mockResolvedValue({ ok: true, count: 2, next_generation: 8 });

    await applyRuntimeSelection(10, [
      { record_id: 2, active: true, value: null, structure: ["Script", "Child"] },
      { record_id: 1, active: true, value: null, structure: ["Script"] },
    ]);

    expect(activeCommands()).toEqual([1, 2]);
  });

  it("waits for an inner script to appear and run before the cheat inside it", async () => {
    // Outer script -> inner script -> cheat, with both inner records missing
    // until the script above them runs. Each is switched on as it appears,
    // outermost first, and the cheat is never sent before its own script.
    const beforeQuery = [query(1, 1, false), query(2, 2, null, false), query(3, 3, null, false)];
    const outerOn = [query(4, 1, true)];
    const innerAppears = [query(5, 2, false), query(6, 3, null, false)];
    const innerOn = [query(7, 2, true)];
    const leafAppears = [query(8, 3, false)];
    const leafOn = [query(9, 3, true)];
    const verified = [query(10, 1, true), query(11, 2, true), query(12, 3, true)];
    const upTo = (...parts: ReturnType<typeof query>[][]) => parts.flat();
    api.getRuntimeStatus
      .mockResolvedValueOnce(envelope(1))
      .mockResolvedValueOnce(envelope(4, beforeQuery))
      .mockResolvedValueOnce(envelope(4, beforeQuery))
      .mockResolvedValueOnce(envelope(5, upTo(beforeQuery, outerOn)))
      .mockResolvedValueOnce(envelope(5, upTo(beforeQuery, outerOn)))
      .mockResolvedValueOnce(envelope(7, upTo(beforeQuery, outerOn, innerAppears)))
      .mockResolvedValueOnce(envelope(7, upTo(beforeQuery, outerOn, innerAppears)))
      .mockResolvedValueOnce(envelope(8, upTo(beforeQuery, outerOn, innerAppears, innerOn)))
      .mockResolvedValueOnce(envelope(8, upTo(beforeQuery, outerOn, innerAppears, innerOn)))
      .mockResolvedValueOnce(envelope(9, upTo(beforeQuery, outerOn, innerAppears, innerOn, leafAppears)))
      .mockResolvedValueOnce(envelope(9, upTo(beforeQuery, outerOn, innerAppears, innerOn, leafAppears)))
      .mockResolvedValueOnce(envelope(10, upTo(beforeQuery, outerOn, innerAppears, innerOn, leafAppears, leafOn)))
      .mockResolvedValueOnce(envelope(10, upTo(beforeQuery, outerOn, innerAppears, innerOn, leafAppears, leafOn)))
      .mockResolvedValue(envelope(13, upTo(beforeQuery, outerOn, innerAppears, innerOn, leafAppears, leafOn, verified)));
    api.writeRuntimeCommands
      .mockResolvedValueOnce({ ok: true, count: 3, next_generation: 4 })
      .mockResolvedValueOnce({ ok: true, count: 1, next_generation: 5 })
      .mockResolvedValueOnce({ ok: true, count: 2, next_generation: 7 })
      .mockResolvedValueOnce({ ok: true, count: 1, next_generation: 8 })
      .mockResolvedValueOnce({ ok: true, count: 1, next_generation: 9 })
      .mockResolvedValueOnce({ ok: true, count: 1, next_generation: 10 })
      .mockResolvedValue({ ok: true, count: 3, next_generation: 13 });

    await applyRuntimeSelection(10, [
      { record_id: 3, active: true, value: null, structure: ["Outer", "Inner", "Cheat"] },
      { record_id: 2, active: true, value: null, structure: ["Outer", "Inner"] },
      { record_id: 1, active: true, value: null, structure: ["Outer"] },
    ]);

    expect(activeCommands()).toEqual([1, 2, 3]);
  });

  it("gives up on a cheat its script never creates, bounded, and says which", async () => {
    // The script ran and the record never appeared: a bounded number of reads,
    // nothing sent to the record that is not there, and a failure naming it.
    const results = [query(1, 1, false), query(2, 2, null, false)];
    const statuses = [envelope(1)];
    const batches: { count: number; next: number }[] = [{ count: 2, next: 3 }];
    statuses.push(envelope(3, [...results]), envelope(3, [...results]));
    results.push(query(3, 1, true));
    batches.push({ count: 1, next: 4 });
    statuses.push(envelope(4, [...results]), envelope(4, [...results]));
    for (let attempt = 0; attempt < 6; attempt += 1) {
      results.push(query(4 + attempt, 2, null, false));
      batches.push({ count: 1, next: 5 + attempt });
      statuses.push(envelope(5 + attempt, [...results]), envelope(5 + attempt, [...results]));
    }
    for (const status of statuses) api.getRuntimeStatus.mockResolvedValueOnce(status);
    api.getRuntimeStatus.mockResolvedValue(statuses.at(-1));
    for (const batch of batches) api.writeRuntimeCommands.mockResolvedValueOnce({ ok: true, count: batch.count, next_generation: batch.next });

    await expect(applyRuntimeSelection(10, [
      { record_id: 2, active: true, value: null, structure: ["Script", "Fly"], label: "Fly" },
      { record_id: 1, active: true, value: null, structure: ["Script"] },
    ])).rejects.toThrow(/Fly did not appear after its enclosing scripts were switched on/);
    expect(activeCommands()).toEqual([1]);
  });

  it("refuses a record that no ancestor in this call could ever create", async () => {
    const beforeQuery = [query(1, 1, null, false)];
    api.getRuntimeStatus
      .mockResolvedValueOnce(envelope(1))
      .mockResolvedValueOnce(envelope(2, beforeQuery));
    api.writeRuntimeCommands.mockResolvedValueOnce({ ok: true, count: 1, next_generation: 2 });

    await expect(applyRuntimeSelection(10, [
      { record_id: 1, active: true, value: null, structure: ["Top"] },
    ])).rejects.toThrow(/does not exist in the running table/);
  });

  it("accepts a descendant that ceased to exist because its own ancestor was switched off", async () => {
    // The intended successful end state used to be indistinguishable from an
    // error: child off, parent off, parent destroys child, child query fails.
    const before = [query(1, 2, true), query(2, 1, true)];
    const mutated = [query(3, 2, false), query(4, 1, false)];
    const verified = [query(5, 2, null, false), query(6, 1, false)];
    api.getRuntimeStatus
      .mockResolvedValueOnce(envelope(1))
      .mockResolvedValueOnce(envelope(3, before))
      .mockResolvedValueOnce(envelope(3, before))
      .mockResolvedValueOnce(envelope(5, [...before, ...mutated]))
      .mockResolvedValueOnce(envelope(5, [...before, ...mutated]))
      .mockResolvedValueOnce(envelope(7, [...before, ...mutated, ...verified]));
    api.writeRuntimeCommands
      .mockResolvedValueOnce({ ok: true, count: 2, next_generation: 3 })
      .mockResolvedValueOnce({ ok: true, count: 2, next_generation: 5 })
      .mockResolvedValueOnce({ ok: true, count: 2, next_generation: 7 });

    await expect(applyRuntimeSelection(10, [
      { record_id: 1, active: false, value: null, structure: ["Script"] },
      { record_id: 2, active: false, value: null, structure: ["Script", "Child"] },
    ])).resolves.toBeTruthy();
  });

  it("returns readable records plus the exact unavailable ones instead of failing the call", async () => {
    const answered = [query(1, 1, true), query(2, 2, null, false)];
    api.getRuntimeStatus
      .mockResolvedValueOnce(envelope(1))
      .mockResolvedValueOnce(envelope(3, answered));
    api.writeRuntimeCommands.mockResolvedValueOnce({ ok: true, count: 2, next_generation: 3 });

    const partial = await queryRuntimeControlsPartial(10, [1, 2]);
    expect(partial.results.map((result) => result.record_id)).toEqual([1]);
    expect(partial.unavailable.map((result) => result.record_id)).toEqual([2]);

    api.getRuntimeStatus.mockReset();
    api.writeRuntimeCommands.mockReset();
    api.getRuntimeStatus
      .mockResolvedValueOnce(envelope(1))
      .mockResolvedValueOnce(envelope(3, answered));
    api.writeRuntimeCommands.mockResolvedValueOnce({ ok: true, count: 2, next_generation: 3 });
    await expect(queryRuntimeControls(10, [1, 2])).rejects.toThrow(/MemoryRecord missing/);
  });

  it("starts a script proved inert by itself once a session, so the values inside it can be read", async () => {
    // `Statistics` only records where its value lives: until it runs, Cheat
    // Engine has no `Gold` to read. The read starts it with a bare command,
    // which confirms nothing about the table, and reads again.
    const controls = [
      { id: 1, description: "Statistics", path: ["Statistics"], structure: ["0"], variable_type: null, kind: "script", group_header: false, has_assembler_script: true, inert_alone: true },
      { id: 2, description: "Gold", path: ["Statistics", "Gold"], structure: ["0", "0"], variable_type: "4 Bytes", kind: "value", group_header: false, has_assembler_script: false },
    ] as any;
    const off = [query(1, 1, false), query(2, 2, null, false)];
    const started = [...off, { generation: 3, record_id: 1, active: true, value: null, ok: true, error: null, error_code: null }];
    const read = [...started, query(4, 1, true), { ...query(5, 2, false), value: "120" }];
    api.getRuntimeStatus
      .mockResolvedValueOnce(envelope(1))
      .mockResolvedValueOnce(envelope(3, off))
      .mockResolvedValueOnce(envelope(3, off))
      .mockResolvedValueOnce(envelope(4, started))
      .mockResolvedValueOnce(envelope(4, started))
      .mockResolvedValueOnce(envelope(6, read));
    api.writeRuntimeCommands
      .mockResolvedValueOnce({ ok: true, count: 2, next_generation: 3 })
      .mockResolvedValueOnce({ ok: true, count: 1, next_generation: 4 })
      .mockResolvedValueOnce({ ok: true, count: 2, next_generation: 6 });

    const answer = await queryReadingInertScripts(10, controls, [1, 2]);

    expect(api.writeRuntimeCommands).toHaveBeenNthCalledWith(2, 10, [{ generation: 3, kind: "set_active", record_id: 1, value: "1" }]);
    expect(answer.results.find((result) => result.record_id === 2)?.value).toBe("120");
    expect(api.confirmTableWorking).not.toHaveBeenCalled();

    // Switched off again by hand in the same session, it stays off: a read
    // starts it once, and does not argue with the user after that.
    api.getRuntimeStatus.mockReset();
    api.writeRuntimeCommands.mockReset();
    api.getRuntimeStatus
      .mockResolvedValueOnce(envelope(6, read))
      .mockResolvedValueOnce(envelope(8, [...read, query(6, 1, false), query(7, 2, null, false)]));
    api.writeRuntimeCommands.mockResolvedValueOnce({ ok: true, count: 2, next_generation: 8 });
    await queryReadingInertScripts(10, controls, [1, 2]);
    expect(api.writeRuntimeCommands).toHaveBeenCalledTimes(1);
  });

  it("starts an inert script on the next read when the closing panel abandoned the read before it was sent", async () => {
    // Tried once a session means sent once. Every script used to be remembered
    // before the first was sent, so one the closing panel never reached stayed
    // off, and its values unread, until Cheat Engine was started again.
    const controls = [
      { id: 11, description: "Statistics", path: ["Statistics"], structure: ["0"], variable_type: null, kind: "script", group_header: false, has_assembler_script: true, inert_alone: true },
      { id: 12, description: "Gold", path: ["Statistics", "Gold"], structure: ["0", "0"], variable_type: "4 Bytes", kind: "value", group_header: false, has_assembler_script: false },
    ] as any;
    const off = [query(1, 11, false), query(2, 12, null, false)];
    const controller = new AbortController();
    api.getRuntimeStatus
      .mockResolvedValueOnce(envelope(1))
      .mockImplementationOnce(async () => { controller.abort(); return envelope(3, off); });
    api.writeRuntimeCommands.mockResolvedValueOnce({ ok: true, count: 2, next_generation: 3 });
    await expect(queryReadingInertScripts(10, controls, [11, 12], controller.signal)).rejects.toBeInstanceOf(RuntimeQueryAbortedError);
    // Only the read went out: nothing was switched on.
    expect(api.writeRuntimeCommands).toHaveBeenCalledTimes(1);

    const started = [...off, { generation: 3, record_id: 11, active: true, value: null, ok: true, error: null, error_code: null }];
    const read = [...started, query(4, 11, true), { ...query(5, 12, false), value: "120" }];
    api.getRuntimeStatus.mockReset();
    api.writeRuntimeCommands.mockReset();
    api.getRuntimeStatus
      .mockResolvedValueOnce(envelope(1))
      .mockResolvedValueOnce(envelope(3, off))
      .mockResolvedValueOnce(envelope(3, off))
      .mockResolvedValueOnce(envelope(4, started))
      .mockResolvedValueOnce(envelope(4, started))
      .mockResolvedValueOnce(envelope(6, read));
    api.writeRuntimeCommands
      .mockResolvedValueOnce({ ok: true, count: 2, next_generation: 3 })
      .mockResolvedValueOnce({ ok: true, count: 1, next_generation: 4 })
      .mockResolvedValueOnce({ ok: true, count: 2, next_generation: 6 });
    const answer = await queryReadingInertScripts(10, controls, [11, 12]);
    expect(api.writeRuntimeCommands).toHaveBeenNthCalledWith(2, 10, [{ generation: 3, kind: "set_active", record_id: 11, value: "1" }]);
    expect(answer.results.find((result) => result.record_id === 12)?.value).toBe("120");

    // And once sent it is tried, whatever came back: a start that fails is not sent again.
    api.getRuntimeStatus.mockReset();
    api.writeRuntimeCommands.mockReset();
    const failing = [
      { id: 21, description: "Other", path: ["Other"], structure: ["1"], variable_type: null, kind: "script", group_header: false, has_assembler_script: true, inert_alone: true },
      { id: 22, description: "Silver", path: ["Other", "Silver"], structure: ["1", "0"], variable_type: "4 Bytes", kind: "value", group_header: false, has_assembler_script: false },
    ] as any;
    const still = [query(1, 21, false), query(2, 22, null, false)];
    // The start's answer never arrived, and Cheat Engine ran it anyway: what
    // comes back is read again, never the read from before the start, which
    // showed a running script as off.
    const ranAnyway = [...still, query(3, 21, true), { ...query(4, 22, false), value: "7" }];
    api.getRuntimeStatus
      .mockResolvedValueOnce(envelope(1))
      .mockResolvedValueOnce(envelope(3, still))
      .mockResolvedValueOnce(envelope(3, still))
      .mockResolvedValueOnce(envelope(3, still))
      .mockResolvedValueOnce(envelope(5, ranAnyway));
    api.writeRuntimeCommands
      .mockResolvedValueOnce({ ok: true, count: 2, next_generation: 3 })
      .mockRejectedValueOnce(new Error("bridge went away"))
      .mockResolvedValueOnce({ ok: true, count: 2, next_generation: 5 });
    const reread = await queryReadingInertScripts(10, failing, [21, 22]);
    expect(api.writeRuntimeCommands).toHaveBeenCalledTimes(3);
    expect(api.writeRuntimeCommands).toHaveBeenNthCalledWith(2, 10, [{ generation: 3, kind: "set_active", record_id: 21, value: "1" }]);
    expect(reread.results.find((result) => result.record_id === 21)?.active).toBe(true);
    expect(reread.results.find((result) => result.record_id === 22)?.value).toBe("7");
    api.getRuntimeStatus.mockReset();
    api.writeRuntimeCommands.mockReset();
    api.getRuntimeStatus.mockResolvedValueOnce(envelope(1)).mockResolvedValueOnce(envelope(3, still));
    api.writeRuntimeCommands.mockResolvedValueOnce({ ok: true, count: 2, next_generation: 3 });
    await queryReadingInertScripts(10, failing, [21, 22]);
    expect(api.writeRuntimeCommands).toHaveBeenCalledTimes(1);
  });

  it("reports a read that fails after a failed start rather than showing the state from before it", async () => {
    const controls = [
      { id: 31, description: "Statistics", path: ["Statistics"], structure: ["0"], variable_type: null, kind: "script", group_header: false, has_assembler_script: true, inert_alone: true },
      { id: 32, description: "Gold", path: ["Statistics", "Gold"], structure: ["0", "0"], variable_type: "4 Bytes", kind: "value", group_header: false, has_assembler_script: false },
    ] as any;
    const off = [query(1, 31, false), query(2, 32, null, false)];
    api.getRuntimeStatus
      .mockResolvedValueOnce(envelope(1))
      .mockResolvedValueOnce(envelope(3, off))
      .mockResolvedValueOnce(envelope(3, off))
      .mockResolvedValueOnce(envelope(3, off));
    api.writeRuntimeCommands
      .mockResolvedValueOnce({ ok: true, count: 2, next_generation: 3 })
      .mockRejectedValueOnce(new Error("bridge went away"))
      .mockRejectedValueOnce(new Error("bridge still away"));
    // Cheat Engine may hold the script on, so "off" would be a claim nobody read.
    await expect(queryReadingInertScripts(10, controls, [31, 32])).rejects.toThrow(/bridge still away/);
  });

  it("does not reinterpret target detachment as an unavailable nested record", async () => {
    const detached = [{
      generation: 1, record_id: 1, active: null, value: null, ok: false,
      error: "target process is not attached", error_code: "target_detached",
    }];
    api.getRuntimeStatus
      .mockResolvedValueOnce(envelope(1))
      .mockResolvedValueOnce(envelope(2, detached));
    api.writeRuntimeCommands.mockResolvedValueOnce({ ok: true, count: 1, next_generation: 2 });

    await expect(queryRuntimeControlsPartial(10, [1])).rejects.toThrow(/not attached/);
  });

  it("does not report Disable all success when record queries lost the target", async () => {
    const detached = [{
      generation: 1, record_id: 1, active: null, value: null, ok: false,
      error: "target process is not attached", error_code: "target_detached",
    }];
    api.getRuntimeStatus
      .mockResolvedValueOnce(envelope(1))
      .mockResolvedValueOnce(envelope(2, detached));
    api.writeRuntimeCommands.mockResolvedValueOnce({ ok: true, count: 1, next_generation: 2 });

    await expect(deactivateAllActiveControls(10, [1])).rejects.toThrow(/not attached/);
    expect(api.writeRuntimeCommands).toHaveBeenCalledTimes(1);
  });

  it("accepts a desired-off child that was already absent before Apply", async () => {
    const before = [query(1, 2, null, false)];
    const verified = [query(2, 2, null, false)];
    api.getRuntimeStatus
      .mockResolvedValueOnce(envelope(1))
      .mockResolvedValueOnce(envelope(2, before))
      .mockResolvedValueOnce(envelope(2, before))
      .mockResolvedValueOnce(envelope(3, [...before, ...verified]));
    api.writeRuntimeCommands
      .mockResolvedValueOnce({ ok: true, count: 1, next_generation: 2 })
      .mockResolvedValueOnce({ ok: true, count: 1, next_generation: 3 });

    await expect(applyRuntimeSelection(10, [
      { record_id: 2, active: false, value: null, structure: ["Script", "Child"] },
    ])).resolves.toBeTruthy();
  });
});


describe("Disable all and switches already off", () => {
  // A bridge that holds each record's state and applies what it is sent, so
  // what is asserted is what the game would be left holding.
  type Held = { active: boolean; value: string; exists: boolean };
  function fakeBridge(state: Map<number, Held>, onSetActive?: (recordId: number, active: boolean) => void, stuck: ReadonlySet<number> = new Set()) {
    let generation = 1;
    const results: any[] = [];
    api.getRuntimeStatus.mockImplementation(async () => envelope(generation, [...results]));
    api.writeRuntimeCommands.mockImplementation(async (_appId: number, commands: any[]) => {
      for (const command of commands) {
        const record = state.get(command.record_id);
        if (!record || !record.exists) {
          results.push({ generation, record_id: command.record_id, ok: false, active: null, value: null, error: "MemoryRecord missing", error_code: "record_missing" });
        } else {
          if (command.kind === "set_active") {
            record.active = command.value === "1";
            onSetActive?.(command.record_id, record.active);
          }
          if (command.kind === "set_value" && !stuck.has(command.record_id)) record.value = command.value;
          results.push({ generation, record_id: command.record_id, ok: true, active: record.active, value: record.value, error: null });
        }
        generation += 1;
      }
      return { ok: true, count: commands.length, next_generation: generation };
    });
    return () => api.writeRuntimeCommands.mock.calls.flatMap((call: any[]) => call[1])
      .filter((command: any) => command.kind !== "query")
      .map((command: any) => [command.kind, command.record_id, command.value]);
  }

  it("puts a switch that is off but holds its on key back to off, and says it corrected it", async () => {
    // Releasing a freeze leaves the value it was frozen at: off on the switch,
    // on in the game. With nothing active the call used to return at once.
    const state = new Map<number, Held>([[1, { active: false, value: "1", exists: true }]]);
    const sent = fakeBridge(state);

    const result = await deactivateAllActiveControls(10, [1], new Map([[1, "0"]]), new Map([[1, "1"]]));

    expect(sent()).toEqual([["set_value", 1, "0"]]);
    expect(state.get(1)!.value).toBe("0");
    expect(result).toMatchObject({ deactivated: 0, corrected: 1, correctedIds: [1] });
  });

  it("corrects only switches with an allowed off key, never a number, never a record its script took away", async () => {
    const state = new Map<number, Held>([
      [1, { active: true, value: "1", exists: true }],    // an active switch
      [2, { active: false, value: "1", exists: true }],   // off, holding on
      [3, { active: false, value: "50", exists: true }],  // a number, off
      [4, { active: false, value: "1", exists: true }],   // a flag the backend did not allow a key for
      [5, { active: false, value: "00", exists: true }],  // off, in another spelling of its off key
      [9, { active: true, value: "", exists: true }],     // a script
      [8, { active: false, value: "1", exists: true }],   // its flag, gone when it goes off
    ]);
    const sent = fakeBridge(state, (recordId, active) => {
      if (recordId === 9 && !active) state.get(8)!.exists = false;
    });

    const result = await deactivateAllActiveControls(
      10, [8, 1, 2, 3, 4, 5, 9],
      new Map([[1, "0"], [2, "0"], [5, "0"], [8, "0"]]),
      new Map([[1, "1"], [2, "1"], [4, "1"], [5, "1"], [8, "1"]]),
    );

    const writes = sent().filter(([kind]) => kind === "set_value");
    expect(writes).toEqual([["set_value", 1, "0"], ["set_value", 2, "0"]]);
    expect(state.get(3)!.value).toBe("50");
    expect(state.get(4)!.value).toBe("1");
    expect(state.get(5)!.value).toBe("00");
    expect(result).toMatchObject({ deactivatedIds: [1, 9], correctedIds: [2] });
  });

  it("does not call a correction made that did not read back", async () => {
    // The game kept its on value: the write is reported, not counted as made.
    const state = new Map<number, Held>([[1, { active: false, value: "1", exists: true }]]);
    fakeBridge(state, undefined, new Set([1]));

    await expect(deactivateAllActiveControls(10, [1], new Map([[1, "0"]]), new Map([[1, "1"]]))).rejects.toThrow(/could not be put back to off/);
  });
});

describe("ambiguous runtime outcomes", () => {
  it("reports a lost write receipt as unknown rather than a definite failure", async () => {
    // The backend commits the generation before it answers, so a transport
    // failure here can mean the game has already changed.
    api.getRuntimeStatus.mockResolvedValue(envelope(5));
    api.writeRuntimeCommands.mockRejectedValueOnce(new Error("websocket closed"));

    await expect(sendRuntimeCommandAndWait(10, { kind: "query", record_id: 7 }))
      .rejects.toBeInstanceOf(RuntimeOutcomeUnknownError);
  });

  it("still reports a backend rejection as a definite failure", async () => {
    api.getRuntimeStatus.mockResolvedValue(envelope(5));
    const rejected = Object.assign(new Error(""), {
      name: "Python ValueError",
      pythonTraceback: "Traceback (most recent call last):\nValueError: set_value is outside the exact table's read-only dropdown",
    });
    api.writeRuntimeCommands.mockRejectedValueOnce(rejected);

    const caught = await sendRuntimeCommandAndWait(10, { kind: "query", record_id: 7 }).catch((cause) => cause);
    expect(caught).toBeInstanceOf(RuntimeOperationError);
    expect(caught).not.toBeInstanceOf(RuntimeOutcomeUnknownError);
  });

  it("reports a lost status read after an accepted batch as unknown", async () => {
    api.getRuntimeStatus
      .mockResolvedValueOnce(envelope(5))
      .mockRejectedValueOnce(new Error("router closed"));
    api.writeRuntimeCommands.mockResolvedValueOnce({ ok: true, count: 1, next_generation: 6 });

    await expect(sendRuntimeCommandAndWait(10, { kind: "query", record_id: 7 }))
      .rejects.toBeInstanceOf(RuntimeOutcomeUnknownError);
  });
});

describe("live control budget and cancellation", () => {
  it("refuses a live query larger than the controller latency budget allows", async () => {
    const ids = Array.from({ length: MAX_LIVE_CONTROLS + 1 }, (_, index) => index + 1);
    await expect(queryRuntimeControlsPartial(10, ids)).rejects.toThrow(/live control is limited/);
    expect(api.writeRuntimeCommands).not.toHaveBeenCalled();
  });

  it("refuses a bulk deactivation larger than that same budget", async () => {
    const ids = Array.from({ length: MAX_LIVE_CONTROLS + 1 }, (_, index) => index + 1);
    await expect(deactivateAllActiveControls(10, ids)).rejects.toThrow(/live control is limited/);
    expect(api.writeRuntimeCommands).not.toHaveBeenCalled();
  });

  it("stops issuing batches once the screen that owns the query is disposed", async () => {
    const ids = Array.from({ length: 130 }, (_, index) => index + 1);
    const aborter = new AbortController();
    let generation = 1;
    api.getRuntimeStatus.mockImplementation(async () =>
      envelope(generation, ids.map((recordId, index) => ({
        generation: index + 1, record_id: recordId, ok: true, active: false, value: "0", error: null,
      }))));
    api.writeRuntimeCommands.mockImplementation(async (_appId: number, commands: any[]) => {
      // The first batch settles, then the owning UI goes away.
      aborter.abort();
      generation += commands.length;
      return { ok: true, count: commands.length, next_generation: generation };
    });

    await expect(queryRuntimeControlsPartial(10, ids, undefined, aborter.signal))
      .rejects.toBeInstanceOf(RuntimeQueryAbortedError);
    // One in-flight batch settles; the remaining two are never issued.
    expect(api.writeRuntimeCommands).toHaveBeenCalledTimes(1);
  });
});

describe("a flag that waits for the script that allocates it", () => {
  it.each([false, true])("refuses a live value edit after the record went off or missing (missing=%s)", async (missing) => {
    const results = [{ generation: 1, record_id: 7, ok: !missing,
      active: missing ? null : false, value: missing ? null : "2",
      error: missing ? "MemoryRecord missing" : null, error_code: missing ? "record_missing" : null }];
    api.getRuntimeStatus.mockResolvedValueOnce(envelope(1)).mockResolvedValue(envelope(2, results));
    api.writeRuntimeCommands.mockResolvedValue({ ok: true, count: 1, next_generation: 2 });
    await expect(applyRuntimeSelection(10, [{ record_id: 7, active: null, value: "8", requires_active: [7] }]))
      .rejects.toThrow(/no longer enabled/);
    expect(api.writeRuntimeCommands.mock.calls.flatMap((call) => call[1]).every((command: any) => command.kind === "query")).toBe(true);
  });

  it.each([[false, true], [true, false]])("writes a setting only while the script it belongs to still runs (running=%s)", async (running, refused) => {
    // The script is not part of the call, so the preflight has to ask for it.
    const results = [
      { generation: 1, record_id: 21, ok: true, active: false, value: "2", error: null, error_code: null },
      { generation: 2, record_id: 20, ok: true, active: running, value: null, error: null, error_code: null },
    ];
    api.getRuntimeStatus.mockResolvedValueOnce(envelope(1)).mockResolvedValue(envelope(3, results));
    // Past the preflight is all this asks about, so the write itself ends the call.
    api.writeRuntimeCommands.mockImplementation(async (_appId: number, commands: any[]) => {
      if (commands.some((command) => command.kind === "set_value")) throw new Error("stop after the write");
      return { ok: true, count: commands.length, next_generation: commands[0].generation + commands.length };
    });
    const call = applyRuntimeSelection(10, [{ record_id: 21, active: null, value: "8", requires_active: [20], label: "Multiplier" }]);
    if (refused) {
      await expect(call).rejects.toThrow(/script Multiplier belongs to is no longer running/);
    } else {
      await call.catch(() => undefined);
    }
    const commands = api.writeRuntimeCommands.mock.calls.flatMap((item) => item[1]);
    expect(commands.some((command: any) => command.kind === "query" && command.record_id === 20)).toBe(true);
    expect(commands.some((command: any) => command.kind === "set_value" && command.record_id === 21)).toBe(!refused);
  });

  // A table can file a script's flag beside it rather than inside it. The
  // flag's record exists before the script runs and its address does not, so
  // a write sent before the script's activation is a write to nothing.
  function allocatingBridge(scriptActive: { value: boolean }) {
    let generation = 1;
    const results: any[] = [];
    const values = new Map<number, string>([[2, "1"]]);
    api.getRuntimeStatus.mockImplementation(async () => envelope(generation, [...results]));
    api.writeRuntimeCommands.mockImplementation(async (_appId: number, commands: any[]) => {
      for (const command of commands) {
        const record = command.record_id;
        if (command.kind === "set_active" && record === 1) scriptActive.value = command.value === "1";
        const unresolved = record === 2 && !scriptActive.value;
        if (command.kind === "set_value" && record === 2 && !unresolved) values.set(2, command.value);
        const ok = !(unresolved && command.kind === "set_value");
        results.push({
          generation, record_id: record, ok,
          active: record === 1 ? scriptActive.value : false,
          value: record === 1 ? "" : unresolved ? "??" : values.get(2),
          error: ok ? null : "address could not be resolved",
        });
        generation += 1;
      }
      return { ok: true, count: commands.length, next_generation: generation };
    });
    return () => api.writeRuntimeCommands.mock.calls.flatMap((call: any[]) => call[1])
      .filter((command: any) => command.kind !== "query")
      .map((command: any) => [command.kind, command.record_id, command.value]);
  }

  it("writes the flag after the script it waits for, whatever order it was asked in", async () => {
    const scriptActive = { value: false };
    const sent = allocatingBridge(scriptActive);

    await applyRuntimeSelection(10, [
      {
        record_id: 2, active: null, value: "0", structure: ["1"], label: "Flag",
        switch_values: { on: "1", off: "0" }, held_off: true, after: [1],
      },
      { record_id: 1, active: true, value: null, structure: ["0"], label: "Script" },
    ]);

    expect(sent()).toEqual([["set_active", 1, "1"], ["set_value", 2, "0"]]);
  });

  it("waits for nothing a call does not switch on", async () => {
    // The script already running is no reason to hold the write back.
    const scriptActive = { value: true };
    const sent = allocatingBridge(scriptActive);

    await applyRuntimeSelection(10, [
      { record_id: 2, active: null, value: "0", structure: ["1"], switch_values: { on: "1", off: "0" }, after: [1] },
    ]);

    expect(sent()).toEqual([["set_value", 2, "0"]]);
  });
});

describe("one refused cheat among several", () => {
  beforeEach(() => {
    api.getRuntimeStatus.mockReset();
    api.writeRuntimeCommands.mockReset();
    api.confirmTableWorking.mockReset();
    api.confirmTableWorking.mockResolvedValue(true);
  });

  // A bridge holding the records it is given, which answers every command of a
  // batch whatever an earlier one did, as the real one does. A record in
  // `refuses` goes straight back off; one in `createdBy` exists only while the
  // script named for it is on.
  function bridge(present: number[], refuses: number[], createdBy: Record<number, number> = {}) {
    let generation = 1;
    const results: any[] = [];
    const active = new Map(present.map((id) => [id, false]));
    const exists = (id: number) => active.has(id) && (createdBy[id] === undefined || active.get(createdBy[id]) === true);
    api.getRuntimeStatus.mockImplementation(async () => envelope(generation, [...results]));
    api.writeRuntimeCommands.mockImplementation(async (_appId: number, commands: any[]) => {
      for (const command of commands) {
        const id = command.record_id;
        if (!exists(id)) {
          results.push({ generation, record_id: id, ok: false, active: null, value: null, error: "MemoryRecord missing", error_code: "record_missing" });
        } else if (command.kind === "set_active" && command.value === "1" && refuses.includes(id)) {
          results.push({ generation, record_id: id, ok: false, active: false, value: null, error: "activation did not settle", error_code: "activation_rejected" });
        } else {
          if (command.kind === "set_active") active.set(id, command.value === "1");
          results.push({ generation, record_id: id, ok: true, active: active.get(id), value: null, error: null, error_code: null });
        }
        generation += 1;
      }
      return { ok: true, count: commands.length, next_generation: generation };
    });
    return active;
  }

  it("switches the rest on, names the refused one and does not call it the table's", async () => {
    const active = bridge([1, 2, 3], [2]);

    const outcome = await applyRuntimeSelection(10, [
      { record_id: 1, active: true, value: null, label: "Health" },
      { record_id: 2, active: true, value: null, label: "Defence factor" },
      { record_id: 3, active: true, value: null, label: "Money" },
    ]);

    expect([...active]).toEqual([[1, true], [2, false], [3, true]]);
    expect(outcome.refused).toEqual([
      { record_id: 2, label: "Defence factor", reason: "Cheat Engine ran it and it went straight back off." },
    ]);
    // And what did work still counts as the table working.
    expect(api.confirmTableWorking).toHaveBeenCalledWith(10, "b".repeat(64), "session", 1);
  });

  it("names what waited for a refused script, and nothing CE Decky only wrote down under it", async () => {
    bridge([1, 10, 11, 20], [10], { 11: 10 });

    const outcome = await applyRuntimeSelection(10, [
      { record_id: 1, active: true, value: null, label: "Health", structure: ["Health"] },
      { record_id: 10, active: true, value: null, label: "Weapons", structure: ["Weapons"] },
      { record_id: 11, active: true, value: null, label: "Ammo", structure: ["Weapons", "Ammo"] },
      { record_id: 20, active: null, value: "0", label: "Flag", held_off: true, after: [10], switch_values: { on: "1", off: "0" } },
    ]);

    expect(outcome.refused).toEqual([
      { record_id: 10, label: "Weapons", reason: "Cheat Engine ran it and it went straight back off." },
      { record_id: 11, label: "Ammo", reason: "It needs “Weapons”, which did not switch on." },
    ]);
    // The flag that waited for the refused script was never written.
    const written = api.writeRuntimeCommands.mock.calls.flatMap((call: any[]) => call[1])
      .filter((command: any) => command.kind === "set_value").map((command: any) => command.record_id);
    expect(written).not.toContain(20);
  });

  it("still puts the question about the table when only scripts for the refused cheat came on", async () => {
    bridge([10, 11], [11]);

    const failure = await applyRuntimeSelection(10, [
      { record_id: 10, active: true, value: null, label: "Weapons", structure: ["Weapons"] },
      { record_id: 11, active: true, value: null, label: "Ammo", structure: ["Weapons", "Ammo"] },
    ]).catch((cause) => cause);

    expect(failure).toBeInstanceOf(RuntimeOperationError);
    expect(failure.tableRefused).toBe(true);
    expect(failure.message).toContain("“Ammo” did not switch on");
    expect(api.confirmTableWorking).not.toHaveBeenCalled();
  });
});

describe("one runtime batch at a time", () => {
  it("queues a press behind a background read instead of claiming the generation it holds", async () => {
    // The backend refuses a generation it has already been sent; two batches
    // that read the log together claimed the same one, and the press lost.
    let generation = 1;
    const results: any[] = [];
    api.getRuntimeStatus.mockImplementation(async () => envelope(generation, [...results]));
    api.writeRuntimeCommands.mockImplementation(async (_appId: number, commands: any[]) => {
      await Promise.resolve();
      if (commands[0].generation !== generation) throw new Error(`runtime command generation is stale; next generation is ${generation}`);
      for (const command of commands) {
        results.push({ generation, record_id: command.record_id, ok: true, active: command.kind === "set_active", value: "1", error: null });
        generation += 1;
      }
      return { ok: true, count: commands.length, next_generation: generation };
    });
    const read = queryRuntimeControlsPartial(10, [1, 2]);
    const press = sendRuntimeCommandAndWait(10, { kind: "set_active", record_id: 3, value: "1" });
    await expect(press).resolves.toMatchObject({ result: { record_id: 3, ok: true } });
    await expect(read).resolves.toMatchObject({ results: [{ record_id: 1 }, { record_id: 2 }] });
    expect(api.writeRuntimeCommands.mock.calls.map((call: any[]) => call[1][0].generation)).toEqual([1, 3]);
  });
});
