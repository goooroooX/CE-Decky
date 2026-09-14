/**
 * The clock every bounded wait in this panel measures with.
 *
 * `Date.now()` is the wall clock and it is corrected: NTP, a manual change, or
 * a device that booted with a bad clock and learned the real one. A deadline
 * computed from it is not a duration at all. A correction backwards of five
 * minutes turns a twelve second wait for a busy bridge into five minutes of a
 * panel whose every other press is refused with "Another CE Decky operation is
 * still running", because the press holding that latch is still inside its own
 * wait; a correction forwards ends the same wait at once and reports a bridge
 * that was working as gone. This project already treats a persisted moment in
 * its own future as a clock that moved rather than as a fact, in the install
 * helper's webhelper spacing and in the Search marker it loads, so this is the
 * host's known behaviour rather than a hypothetical one.
 *
 * `performance.now()` is monotonic and is what the platform provides for
 * exactly this. It is read through the global rather than through `window`, for
 * the same reason the support-log timers are: this module is evaluated by the
 * packaging smoke check in bare Node as well as by the panel.
 *
 * Wall time stays wall time. A moment that has to line up with a timestamp from
 * somewhere else, a record's `at` or a provider's reported age, is still
 * `Date.now()`, and nothing here changes that.
 */
const clock = globalThis as { performance?: { now?: () => number } };

/** Milliseconds since an arbitrary fixed point, never moved by a clock change. */
export function monotonicNow(): number {
  const now = clock.performance?.now;
  // A host without it is not a host this panel runs on, but the fallback is
  // still the wall clock rather than an exception: a diagnostics-grade wait
  // that throws would be worse than one that can be skewed.
  return typeof now === "function" ? now.call(clock.performance) : Date.now();
}

/** How long since a moment `monotonicNow` returned. */
export function elapsedSince(started: number): number {
  return Math.max(0, Math.round(monotonicNow() - started));
}

/**
 * When this renderer started, as a wall-clock moment.
 *
 * The one value that says which side of a frontend reload a panel row belongs
 * to, and it has to be a moment rather than an age: an age recorded once and
 * compared against how long a reader has been waiting classifies the same
 * fixed record differently on two reads, and a row from the frontend being
 * replaced then turns into the row that replaced it. Two fixed moments, the
 * renderer's start and the reload's request, compare the same way for ever.
 *
 * Wall clock deliberately, because the other side of the comparison is the
 * install helper's own `time.time()` and the two are seconds apart on one
 * machine. A correction inside those seconds is the limit of this, and it is
 * a limit rather than a mechanism: the alternative is another handshake for a
 * case worth less than the machinery it costs.
 */
export function rendererStartedAt(): number {
  return Math.round(Date.now() - monotonicNow());
}
