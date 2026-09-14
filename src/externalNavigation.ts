import { Navigation } from "@decky/ui";

const MAX_EXTERNAL_URL_BYTES = 8192;

/** Open one bounded HTTPS page through Decky's current navigation surface. */
export function openExternalWeb(url: string): void {
  if (typeof url !== "string" || new TextEncoder().encode(url).length > MAX_EXTERNAL_URL_BYTES) {
    throw new Error("External page URL is invalid or too long.");
  }
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    throw new Error("External page URL is invalid.");
  }
  if (parsed.protocol !== "https:" || parsed.username || parsed.password) {
    throw new Error("External pages must use HTTPS without embedded credentials.");
  }
  const normalized = parsed.toString();
  if (new TextEncoder().encode(normalized).length > MAX_EXTERNAL_URL_BYTES) {
    throw new Error("External page URL is invalid or too long.");
  }
  if (typeof Navigation?.NavigateToExternalWeb === "function") {
    Navigation.NavigateToExternalWeb(normalized);
    return;
  }
  window.open(normalized, "_blank", "noopener,noreferrer");
}
