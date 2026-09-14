import { describe, expect, it } from "vitest";

import { isDeckyFilePickerCancellation } from "../src/deckyFilePicker";

describe("Decky file-picker cancellation", () => {
  it("accepts only Decky Loader's explicit cancellation signal", () => {
    expect(isDeckyFilePickerCancellation("User canceled")).toBe(true);
    expect(isDeckyFilePickerCancellation(new Error("User canceled"))).toBe(true);
    expect(isDeckyFilePickerCancellation("user canceled")).toBe(false);
    expect(isDeckyFilePickerCancellation(new Error("permission denied"))).toBe(false);
    expect(isDeckyFilePickerCancellation({ message: "User canceled" })).toBe(false);
  });
});
