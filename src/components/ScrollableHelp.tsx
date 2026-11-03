import { Focusable, ScrollPanelGroup, scrollPanelClasses } from "@decky/ui";
import { useLayoutEffect, useRef, useState, type ComponentType, type CSSProperties, type ReactNode, type RefAttributes } from "react";
import { traceUiAction } from "../uiActions";

// Steam's panel takes a ref and a style, and its `Focusable` takes `focusable`
// and `noFocusRing`; the library's declarations name none of them.
const SteamScrollPanel = ScrollPanelGroup as unknown as ComponentType<
  { children?: ReactNode; focusable?: boolean; style?: CSSProperties } & RefAttributes<HTMLElement>
> | undefined;
const SteamFocusable = Focusable as unknown as ComponentType<
  { children?: ReactNode; focusable?: boolean; noFocusRing?: boolean }
>;

const FRAME: CSSProperties = {
  maxHeight: "min(40vh, 300px)",
  margin: "2px 0",
  borderLeft: "3px solid hsla(210, 70%, 68%, 0.35)",
};
const FRAME_PANEL: CSSProperties = { ...FRAME, display: "flex", flexDirection: "column" };
const FRAME_PLAIN: CSSProperties = { ...FRAME, overflowY: "auto" };
const PANEL: CSSProperties = { flex: 1, minHeight: 0 };
const LINE: CSSProperties = { padding: "2px 8px" };
const CLASS = "ce-decky-help-scroll";
// The rule brightens while focus is in the box, which is the whole of what
// says so: its one stop carries no ring, so that Steam's ring has nothing to
// travel to inside a box that scrolls. A thin scrollbar says there is more.
const css = (panel: string | undefined) => [
  `.${CLASS}:has(.gpfocuswithin), .${CLASS}:has(.gpfocus) { border-left-color: hsla(210, 85%, 74%, 0.95); }`,
  ...(panel ? [
    `.${CLASS} .${panel}::-webkit-scrollbar { display: initial !important; width: 4px; }`,
    `.${CLASS} .${panel}::-webkit-scrollbar-thumb { border: 0; border-radius: 2px; background-color: hsla(210, 70%, 68%, 0.45); }`,
  ] : []),
].join("\n");

/**
 * A row's help that can run longer than a handheld's screen, read with a D-pad.
 *
 * `PanelRow` draws help as a plain field, which Steam cannot put focus on, so
 * text past the bottom of an 800 pixel screen was out of reach: the D-pad moves
 * between controls, not through a paragraph. This is the shape the update
 * window's notes use, Steam's own scroll panel holding one stop with no ring,
 * and `docs/FIELD_NOTES.md` names the plugins it was read from. That stop
 * exists only while the text overflows its box, so help that fits adds nothing
 * to walk past. Where Steam's panel was not found, each line is a stop of its
 * own in a box the browser scrolls, because Steam moves a view only by moving
 * focus.
 */
export function ScrollableHelp({ lines, testId }: { lines: readonly string[]; testId?: string }) {
  const panelRef = useRef<HTMLElement | null>(null);
  // Unmeasurable counts as overflowing: help nobody can reach is worse than one
  // stop too many.
  const [overflowing, setOverflowing] = useState(true);
  const text = lines.join("\n");
  useLayoutEffect(() => {
    const panel = panelRef.current;
    if (!panel || !panel.clientHeight) return;
    setOverflowing(panel.scrollHeight > panel.clientHeight);
  }, [text]);
  const body = (stops: boolean) => lines.map((line, index) => (stops
    ? <Focusable key={index} onActivate={traceUiAction("scrollable_help.line", () => undefined)}><div style={LINE}>{line}</div></Focusable>
    : <div key={index} style={LINE}>{line}</div>));
  if (!SteamScrollPanel) {
    return (
      <div className={CLASS} data-testid={testId} style={FRAME_PLAIN}>
        <style>{css(undefined)}</style>
        {body(true)}
      </div>
    );
  }
  return (
    <div className={CLASS} data-testid={testId} style={FRAME_PANEL}>
      <style>{css(scrollPanelClasses?.ScrollPanel)}</style>
      <SteamScrollPanel ref={panelRef} focusable={false} style={PANEL}>
        <SteamFocusable noFocusRing focusable={overflowing}>
          {body(false)}
        </SteamFocusable>
      </SteamScrollPanel>
    </div>
  );
}
