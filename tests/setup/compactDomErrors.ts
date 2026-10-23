/**
 * What a failed query prints: the page's rows and their text, not its markup.
 *
 * Testing Library answers a query that found nothing with the whole container
 * as indented markup, cut at a few thousand characters. This plugin's panel
 * carries its own stylesheet and deep Steam-shaped wrappers, so the cut landed
 * inside the first card and the rows a failure is about were never printed;
 * reading them meant adding a temporary log, which then filled with the
 * stylesheet's text. A row here is its `data-testid` and the words in it,
 * which is what the queries that failed were looking for.
 */
import { configure } from "@testing-library/react";

const OUTLINE_LIMIT = 6000;
const ROW_TEXT_LIMIT = 200;

function visibleText(node: Node): string {
  const clone = node.cloneNode(true) as Element;
  if (typeof clone.querySelectorAll === "function") {
    clone.querySelectorAll("style, script").forEach((child) => child.remove());
  }
  return (clone.textContent ?? "").replace(/\s+/g, " ").trim();
}

function clip(text: string, limit: number): string {
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

export function pageOutline(container: Element | null | undefined): string {
  if (!container) return "page: (no container)";
  const rows = [...container.querySelectorAll("[data-testid]")].map((node) =>
    `  [${node.getAttribute("data-testid")}] ${clip(visibleText(node), ROW_TEXT_LIMIT)}`);
  const lines = [
    `rows with a data-testid (${rows.length}):`,
    ...rows,
    "page text:",
    `  ${visibleText(container)}`,
  ];
  return clip(lines.join("\n"), OUTLINE_LIMIT);
}

configure({
  getElementError(message, container) {
    const error = new Error([message ?? "", pageOutline(container as Element | null)].join("\n\n"));
    error.name = "TestingLibraryElementError";
    return error;
  },
});
