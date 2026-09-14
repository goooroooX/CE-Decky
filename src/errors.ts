/**
 * One human-readable sentence for anything that can reach a toast or a Field.
 *
 * Decky Loader 3.2.6 loses the backend message on the way to the frontend: its
 * `WSRouter._call_route()` sends `{name, message, traceback}` while
 * `wsrouter.ts` builds `new PyError(data.error.name, data.error.error, ...)`,
 * so `PyError.message` is the empty string for *every* backend exception and
 * CE Decky showed an empty notification instead of the cause. The exact Python
 * text still arrives inside `pythonTraceback`, whose last line is
 * `<ExceptionClass>: <message>`, so recover it from there and fall back to the
 * exception class only when even that is unavailable.
 */

const GENERIC_FALLBACK = "CE Decky failed without reporting a reason.";
const PYTHON_NAME_PREFIX = "Python ";

interface PythonErrorLike {
  pythonTraceback?: unknown;
}

/** Last `<ExceptionClass>: <message>` line of a Python traceback, if any. */
export function pythonTracebackSummary(traceback: unknown): string | null {
  if (typeof traceback !== "string") return null;
  const lines = traceback.split("\n").map((line) => line.trimEnd());
  for (let index = lines.length - 1; index >= 0; index -= 1) {
    const line = lines[index].trim();
    if (!line || line.startsWith("Traceback (") || lines[index].startsWith("  ")) continue;
    const separator = line.indexOf(": ");
    if (separator > 0) {
      const exceptionClass = line.slice(0, separator);
      const message = line.slice(separator + 2).trim();
      // Only treat this as an exception line when the left side really looks
      // like a dotted Python class name; a stray sentence with a colon is not.
      if (message && /^[A-Za-z_][A-Za-z0-9_.]*$/.test(exceptionClass)) return message;
    }
    if (/^[A-Za-z_][A-Za-z0-9_.]*$/.test(line)) return line;
    return line;
  }
  return null;
}

/**
 * The exception class of a Python traceback, without its module path.
 *
 * The message is what a person reads; the class is what the backend uses to say
 * which kind of failure this was. A durable write that failed after the new
 * content was already in place is a different fact from one the backend refused
 * outright, and only the class carries it across the RPC boundary.
 */
export function pythonExceptionClass(traceback: unknown): string | null {
  if (typeof traceback !== "string") return null;
  const lines = traceback.split("\n").map((line) => line.trimEnd());
  for (let index = lines.length - 1; index >= 0; index -= 1) {
    const line = lines[index].trim();
    if (!line || line.startsWith("Traceback (") || lines[index].startsWith("  ")) continue;
    const separator = line.indexOf(": ");
    const candidate = separator > 0 ? line.slice(0, separator) : line;
    if (!/^[A-Za-z_][A-Za-z0-9_.]*$/.test(candidate)) return null;
    const parts = candidate.split(".");
    return parts[parts.length - 1];
  }
  return null;
}

export function describeError(cause: unknown, fallback: string = GENERIC_FALLBACK): string {
  if (typeof cause === "string" && cause.trim()) return cause.trim();
  if (cause instanceof Error) {
    const message = typeof cause.message === "string" ? cause.message.trim() : "";
    if (message) return message;
    const recovered = pythonTracebackSummary((cause as unknown as PythonErrorLike).pythonTraceback);
    if (recovered) return recovered;
    const name = typeof cause.name === "string" ? cause.name.trim() : "";
    if (name && name !== "Error") {
      // `PyError` names itself `Python ValueError`; report the exception rather
      // than nothing when no message survived the loader.
      return name.startsWith(PYTHON_NAME_PREFIX)
        ? `${name.slice(PYTHON_NAME_PREFIX.length)} (the backend reported no message)`
        : name;
    }
    return fallback;
  }
  if (cause === null || cause === undefined) return fallback;
  const text = String(cause).trim();
  if (!text || text === "[object Object]") return fallback;
  return text;
}
