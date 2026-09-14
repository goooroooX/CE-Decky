import { afterEach, describe, expect, it, vi } from "vitest";

import { requestPanelFocus, subscribePanelFocus, takePanelFocus } from "../src/panelFocus";

// One module instance is shared by every case here, exactly as one is shared by
// every panel Steam builds. Taking the request is what retires it, so that is
// how a case leaves nothing behind for the next one.
afterEach(() => { takePanelFocus("search"); });

describe("the control an answer hands the panel back at", () => {
  it("outlives the panel that asked, and is taken by whichever one can act on it", () => {
    // Nothing was asked for, so nothing is taken: a panel opened for any other
    // reason keeps the focus Steam gives it.
    expect(takePanelFocus("search")).toBe(false);
    requestPanelFocus("search");
    expect(takePanelFocus("search")).toBe(true);
  });

  it("is retired by the panel that takes it, so one answer moves the ring once", () => {
    requestPanelFocus("search");
    expect(takePanelFocus("search")).toBe(true);
    // The panel is rebuilt several times over a session, and none of those is
    // an answer about a table.
    expect(takePanelFocus("search")).toBe(false);
  });

  it("stays outstanding for a panel that could not act on it yet", () => {
    requestPanelFocus("search");
    // Search is disabled while the panel is busy and while it has no game, and
    // the answer is still writing when the request arrives. A panel that asks
    // and cannot act does not ask again; it never took it in the first place,
    // and the one that can is the one that takes it.
    expect(takePanelFocus("search")).toBe(true);
    requestPanelFocus("search");
    requestPanelFocus("search");
    // Two answers in a row are still one ring to move.
    expect(takePanelFocus("search")).toBe(true);
    expect(takePanelFocus("search")).toBe(false);
  });

  it("tells a panel that is already on screen, without carrying any state to it", () => {
    const heard = vi.fn(() => {
      // The listener is told that asking is now worth it, and asks.
      expect(takePanelFocus("search")).toBe(true);
    });
    const stop = subscribePanelFocus(heard);
    requestPanelFocus("search");
    expect(heard).toHaveBeenCalledTimes(1);
    stop();
    requestPanelFocus("search");
    expect(heard).toHaveBeenCalledTimes(1);
  });

  it("keeps one panel's failure from silencing another, and leaves the request pending", () => {
    const second = vi.fn();
    const stopFirst = subscribePanelFocus(() => { throw new Error("panel is unmounting"); });
    const stopSecond = subscribePanelFocus(second);
    expect(() => requestPanelFocus("search")).not.toThrow();
    expect(second).toHaveBeenCalledTimes(1);
    expect(takePanelFocus("search")).toBe(true);
    stopFirst();
    stopSecond();
  });

  it("lets a listener unsubscribe from inside its own handler", () => {
    // A panel unmounting inside its own re-read is an ordinary way for the set
    // to change while it is being walked.
    const later = vi.fn();
    let stopSelf = () => undefined as void;
    stopSelf = subscribePanelFocus(() => stopSelf());
    const stopLater = subscribePanelFocus(later);
    expect(() => requestPanelFocus("search")).not.toThrow();
    expect(later).toHaveBeenCalledTimes(1);
    stopLater();
  });
});
