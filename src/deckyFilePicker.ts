/** Return true only for Decky Loader's explicit file-picker cancellation signal. */
export function isDeckyFilePickerCancellation(reason: unknown): boolean {
  if (reason === "User canceled") return true;
  return reason instanceof Error && reason.message === "User canceled";
}
