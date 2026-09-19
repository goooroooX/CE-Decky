import { describe, expect, it } from "vitest";
import { DELETION_REFUSED_PREFIX, refusedBeforeDeleting } from "../src/managedDeletion";

describe("telling a refused deletion from a lost answer", () => {
  it("recognises the refusal the backend sends before it touches anything", () => {
    expect(refusedBeforeDeleting(new Error(`${DELETION_REFUSED_PREFIX}a table download is still in progress; cancel it first`))).toBe(true);
    // The other half of this constant is `DELETION_REFUSED_PREFIX` in
    // `py_modules/ce_decky/service.py`, and a test there holds it to the same
    // words.
    expect(DELETION_REFUSED_PREFIX).toBe("nothing was deleted; ");
  });

  it("treats everything else as an outcome it cannot be sure of", () => {
    // A deletion commits before the call returns, so a lost answer is not a
    // deletion that did not happen and this side must reconcile itself.
    expect(refusedBeforeDeleting(new Error("deletion reply lost"))).toBe(false);
    expect(refusedBeforeDeleting(new Error("a Cheat Engine process CE Decky owns is still running"))).toBe(false);
    expect(refusedBeforeDeleting(undefined)).toBe(false);
    expect(refusedBeforeDeleting("plain text")).toBe(false);
  });
});
