import { pathToFileURL } from "node:url";
import { resolve } from "node:path";

globalThis.window = {
  __DECKY_SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED_deckyLoaderAPIInit: {
    connect: () => ({
      _version: 2,
      callable: (name) => async () => ({ name }),
      openFilePicker: async () => ({}),
      toaster: { toast() {} },
    }),
  },
};
globalThis.DFL = { staticClasses: { Title: "title" } };
globalThis.SP_REACT = {
  createElement(type, props, ...children) { return { type, props: { ...(props ?? {}), children } }; },
  Fragment: Symbol("Fragment"),
  // Steam's own React is what `SP_REACT` is at runtime, so it carries the full
  // API. The bundle subclasses `Component` for the error boundary that records
  // a render crash, and a stub without it fails at module evaluation - which is
  // the smoke test reporting its own incompleteness, not a defect in the bundle.
  Component: class Component {
    constructor(props) { this.props = props; this.state = {}; }
    setState(next) { this.state = { ...this.state, ...next }; }
    render() { return null; }
  },
};
globalThis.SP_JSX = {
  jsx(type, props, key) { return { type, key, props: props ?? {} }; },
  jsxs(type, props, key) { return { type, key, props: props ?? {} }; },
  Fragment: Symbol("Fragment"),
};

const url = `${pathToFileURL(resolve("dist/index.js")).href}?smoke=${Date.now()}`;
const mod = await import(url);
if (typeof mod.default !== "function") throw new Error("fallback dist default export is not a plugin factory");
const plugin = mod.default();
if (!plugin || plugin.name !== "CE Decky") throw new Error("fallback dist plugin factory returned an invalid plugin");
console.log("fallback dist import smoke: PASS");
