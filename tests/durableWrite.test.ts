import { describe, expect, it, vi } from "vitest";
import {
  DurableOutcomeUnknownError,
  PriorDurableCommitError,
  commitDesiredState,
  configuredValuesMatch,
  describeCommitFailure,
  durableResidue,
  rememberedMatches,
} from "../src/durableWrite";

function backendRejection(message: string): Error {
  // Decky loses the message on the way to the frontend but preserves the
  // Python traceback, which is what proves the backend itself refused.
  return Object.assign(new Error(""), {
    name: "Python ValueError",
    pythonTraceback: `Traceback (most recent call last):\nValueError: ${message}`,
  });
}

function postCommitFailure(message: string): Error {
  // What `atomic_write_bytes` raises after `os.replace` has already published
  // the new content and the directory sync then failed.
  return Object.assign(new Error(""), {
    name: "Python DurabilityUnknownError",
    pythonTraceback: `Traceback (most recent call last):\nce_decky.atomic.DurabilityUnknownError: ${message}`,
  });
}

describe("commitDesiredState", () => {
  it("reconciles a backend failure raised after the content was already published", async () => {
    // `os.replace` publishes the replacement before the directory entry is
    // synced, so this one traceback is not proof the backend refused: the new
    // state is already the one every reader sees. Treating it as a refusal
    // reported a saved profile as unsaved and offered a retry for it.
    const verify = vi.fn(async () => true);
    await commitDesiredState({
      subject: "the target process",
      write: async () => { throw postCommitFailure("profiles.json was replaced and the directory could not be synced"); },
      verify,
    });
    expect(verify).toHaveBeenCalledOnce();
  });

  it("never calls a post-commit failure a definite refusal, even unverified", async () => {
    // The content was published when the backend raised, so an authority that
    // does not hold it is a contradiction rather than "nothing was saved".
    await expect(commitDesiredState({
      subject: "the target process",
      write: async () => { throw postCommitFailure("directory sync failed"); },
      verify: async () => false,
    })).rejects.toBeInstanceOf(DurableOutcomeUnknownError);
  });

  it("returns without consulting the authority when the write is acknowledged", async () => {
    const verify = vi.fn();
    await commitDesiredState({ subject: "state", write: async () => undefined, verify });
    expect(verify).not.toHaveBeenCalled();
  });

  it("treats a lost reply as committed when the exact state is present afterwards", async () => {
    // The backend saves atomically and the result is fully inspectable, so a
    // dropped response is reconcilable rather than proof nothing happened.
    const cause = new Error("websocket closed");
    await commitDesiredState({
      subject: "state",
      write: async () => { throw cause; },
      verify: async () => true,
    });
  });

  it("still fails when the authority says the state is not there", async () => {
    const cause = new Error("websocket closed");
    await expect(commitDesiredState({
      subject: "state",
      write: async () => { throw cause; },
      verify: async () => false,
    })).rejects.toBe(cause);
  });

  it("never reconciles a rejection the backend itself raised", async () => {
    const verify = vi.fn();
    const cause = backendRejection("value is outside the exact table's read-only dropdown");
    await expect(commitDesiredState({ subject: "state", write: async () => { throw cause; }, verify }))
      .rejects.toBe(cause);
    expect(verify).not.toHaveBeenCalled();
  });

  it("reports an unknown outcome when the authority cannot be read either", async () => {
    await expect(commitDesiredState({
      subject: "this table's configured values",
      write: async () => { throw new Error("websocket closed"); },
      verify: async () => { throw new Error("router closed"); },
    })).rejects.toBeInstanceOf(DurableOutcomeUnknownError);
  });
});

describe("exact desired-state comparison", () => {
  it("matches configured values only as an exact set", () => {
    const desired = [{ record_id: 1, value: "10" }, { record_id: 2, value: "20" }];
    expect(configuredValuesMatch([...desired].reverse(), desired)).toBe(true);
    expect(configuredValuesMatch([{ record_id: 1, value: "10" }], desired)).toBe(false);
    expect(configuredValuesMatch([...desired, { record_id: 3, value: "30" }], desired)).toBe(false);
    expect(configuredValuesMatch([{ record_id: 1, value: "11" }, { record_id: 2, value: "20" }], desired)).toBe(false);
    expect(configuredValuesMatch(undefined, [])).toBe(true);
  });

  it("matches remembered state on active and value together", () => {
    const desired = [{ record_id: 7, active: true, value: "100" }];
    expect(rememberedMatches(desired, desired)).toBe(true);
    expect(rememberedMatches([{ record_id: 7, active: false, value: "100" }], desired)).toBe(false);
    expect(rememberedMatches([{ record_id: 7, active: true, value: "101" }], desired)).toBe(false);
    expect(rememberedMatches([], desired)).toBe(false);
  });
});

describe("durableResidue", () => {
  it("reports nothing durable for an ordinary rejection", () => {
    expect(durableResidue(new Error("refused"))).toEqual({ committed: false, unknown: false });
  });

  it("reports the unestablished outcome of a single write", () => {
    expect(durableResidue(new DurableOutcomeUnknownError("could not confirm"))).toEqual({
      committed: false, unknown: true,
    });
  });

  it("reports the earlier write of a multi-write action as committed", () => {
    // The second mutation of one Apply being refused is not proof that the
    // first did not happen: grouping them made a backend traceback from the
    // later write stand for the earlier one, and the stored selection was
    // reported as unsaved.
    const wrapped = new PriorDurableCommitError(backendRejection("autoload refused"), "The cheat selection");
    expect(durableResidue(wrapped)).toEqual({ committed: true, unknown: false });
    expect(wrapped.message).toContain("The cheat selection was already saved and stays saved.");
  });

  it("keeps both facts when the later write's own outcome is unknown", () => {
    const wrapped = new PriorDurableCommitError(
      new DurableOutcomeUnknownError("could not confirm"), "The cheat selection",
    );
    expect(durableResidue(wrapped)).toEqual({ committed: true, unknown: true });
  });
});

describe("describeCommitFailure", () => {
  const wording = {
    definite: "The cheats were switched off, but that was not remembered:",
    unknown: "The cheats were switched off.",
  };

  it("adds the caller's definite context to a backend refusal", () => {
    const relabelled = describeCommitFailure(backendRejection("budget exceeded"), wording);
    expect(relabelled).not.toBeInstanceOf(DurableOutcomeUnknownError);
    expect(relabelled.message).toContain("was not remembered");
    expect(relabelled.message).toContain("budget exceeded");
  });

  it("keeps an unestablished outcome unestablished", () => {
    // Prefixing this with "it was not remembered" claimed exactly what the
    // helper had just said it could not establish, in the same sentence.
    const cause = new DurableOutcomeUnknownError(
      "Transport lost. CE Decky could not confirm whether the cheats were saved; refresh before deciding what to do.",
    );
    const relabelled = describeCommitFailure(cause, wording);
    expect(relabelled).toBeInstanceOf(DurableOutcomeUnknownError);
    expect(relabelled.message).not.toContain("was not remembered");
    expect(relabelled.message).toBe(`The cheats were switched off. ${cause.message}`);
  });

  it("keeps the residue helper working on the relabelled error", () => {
    const relabelled = describeCommitFailure(new DurableOutcomeUnknownError("unclear"), wording);
    expect(durableResidue(relabelled)).toEqual({ committed: false, unknown: true });
  });
});
