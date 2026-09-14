import { useEffect, useRef } from "react";
import { logUi } from "./supportLog";

let sequence = 0;

/** Also observes a host-driven unmount that did not call one of our buttons. */
export function useUiSurface(surface: string, identity?: string | number | null): void {
  const instance = useRef<number | null>(null);
  if (instance.current === null) instance.current = ++sequence;
  useEffect(() => {
    const fields = { surface, identity, surface_instance: instance.current };
    logUi("ui.surface_opened", fields);
    return () => logUi("ui.surface_closed", fields);
  }, [surface, identity]);
}
