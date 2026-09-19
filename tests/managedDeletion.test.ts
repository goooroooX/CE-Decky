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

  it("recognises it on the loader that loses the message", () => {
    // Decky Loader 3.2.6 delivers every backend exception with an empty
    // `PyError.message` and the Python text only in `pythonTraceback`, which is
    // what `src/errors.ts` exists for. Matching the message alone made every
    // refusal on that loader look like an outcome nobody could be sure of.
    const pyError = Object.assign(new Error(""), {
      name: "Python ValueError",
      pythonTraceback: [
        "Traceback (most recent call last):",
        '  File "/home/deck/homebrew/plugins/CE-Decky/py_modules/ce_decky/service.py", line 2406, in _delete_managed_data',
        "    raise ValueError(_refused_before_deleting(refusal))",
        `ValueError: ${DELETION_REFUSED_PREFIX}a plugin update is still in progress; wait for it to finish`,
      ].join("\n"),
    });
    expect(refusedBeforeDeleting(pyError)).toBe(true);
  });

  it("treats everything else as an outcome it cannot be sure of", () => {
    // A deletion commits before the call returns, so a lost answer is not a
    // deletion that did not happen and this side must reconcile itself.
    expect(refusedBeforeDeleting(new Error("deletion reply lost"))).toBe(false);
    // Including a Python traceback that is not a refusal: a deletion can fail
    // after it has begun, and that is an outcome this side must reconcile.
    expect(refusedBeforeDeleting(Object.assign(new Error(""), {
      name: "Python OSError",
      pythonTraceback: "Traceback (most recent call last):\nOSError: [Errno 5] Input/output error",
    }))).toBe(false);
    expect(refusedBeforeDeleting(new Error("a Cheat Engine process CE Decky owns is still running"))).toBe(false);
    expect(refusedBeforeDeleting(undefined)).toBe(false);
    expect(refusedBeforeDeleting("plain text")).toBe(false);
  });
});
