import { beforeEach, describe, expect, it, vi } from "vitest";
import { currentUiAction, startUiOperation, traceUiAction, traceUiEdit } from "../src/uiActions";
import { readSupportLog, resetSupportLog } from "../src/supportLog";

beforeEach(() => { resetSupportLog(); vi.useRealTimers(); });

describe("user action evidence", () => {
  it("preserves arguments and returns without inspecting secrets or DOM events", () => {
    const secret = { get password() { throw new Error("must never inspect arguments"); } };
    const handler = vi.fn((_arg: unknown) => 42);
    expect(traceUiAction("archive.import", handler, { table_sha: "a".repeat(64) })(secret)).toBe(42);
    expect(handler.mock.calls[0][0]).toBe(secret);
    expect(readSupportLog().entries).toMatchObject([{ event: "ui.action", fields: { action: "archive.import", table_sha: "a".repeat(64) } }]);
    expect(JSON.stringify(readSupportLog())).not.toContain("password");
  });

  it("correlates overlapping operations without leaking an interaction into background work", async () => {
    let finish!: () => void;
    const pending = new Promise<void>((resolve) => { finish = resolve; });
    const a = traceUiAction("manage.delete", async () => {
      const op = startUiOperation("table.delete", { table_sha: "a".repeat(64) });
      await pending;
      op.completed();
    })();
    expect(currentUiAction()).toBeUndefined();
    traceUiAction("advanced.refresh", () => startUiOperation("status").completed())();
    startUiOperation("background.poll").completed();
    finish(); await a;
    const events = readSupportLog().entries;
    const press = events.find((entry) => entry.fields.action === "manage.delete")!;
    const terminal = events.find((entry) => entry.event === "ui.operation_completed" && entry.fields.operation === "table.delete")!;
    expect(terminal.fields.interaction).toBe(press.fields.interaction);
    expect(events.find((entry) => entry.fields.operation === "background.poll")!.fields.interaction).toBeUndefined();
  });

  it("records synchronous and asynchronous callback failures without changing their outcome", async () => {
    const cause = new Error("write refused");
    expect(() => traceUiAction("sync", () => { throw cause; })()).toThrow(cause);
    expect(() => traceUiEdit("edit", () => { throw cause; })()).toThrow(cause);
    const pending = Promise.reject(cause);
    const returned = traceUiAction("async", () => pending)();
    expect(returned).toBe(pending);
    await expect(returned).rejects.toBe(cause);
    expect(readSupportLog().entries.filter((entry) => entry.event === "ui.handler_failed")).toHaveLength(3);
    expect(readSupportLog().entries.some((entry) => entry.event === "ui.operation_completed")).toBe(false);
  });

  it("coalesces long typing bursts and starts a fresh entry after another action", () => {
    const input = traceUiEdit("archive.password", vi.fn());
    for (let i = 0; i < 10000; i++) input({ target: { value: "secret" } });
    traceUiAction("archive.select", () => undefined)();
    input({ target: { value: "another-secret" } });
    expect(readSupportLog().entries.map((entry) => entry.event)).toEqual(["ui.edit", "ui.action", "ui.edit"]);
    expect(JSON.stringify(readSupportLog())).not.toContain("secret");
    expect(readSupportLog().dropped).toBe(0);
  });
});
