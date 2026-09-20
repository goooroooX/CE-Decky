const DERIVED_LABEL = "CE Decky made this copy from another table";

/**
 * One passive glyph for bytes this device produced rather than received.
 *
 * Provenance, never status, and drawn in the panel's accent for the same reason
 * `SameTableMark` is: a row can carry a compatibility verdict beside it and the
 * two must not read as one finding. What it means is on the row's own line,
 * which names the table these bytes came from.
 */
export function DerivedMark() {
  return <span
    role="img"
    title={DERIVED_LABEL}
    aria-label={DERIVED_LABEL}
    style={{ color: "var(--ce-accent, hsla(203, 89%, 66%, 0.85))", whiteSpace: "nowrap", flexShrink: 0, lineHeight: 0 }}
  >
    <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true" focusable="false" style={{ verticalAlign: "middle" }}>
      {/* One sheet made from another: the arrow leaves the first and lands on
          the second, which is the direction the row's line reads in. */}
      <rect x="1.5" y="3" width="5.5" height="10" rx="1" fill="none" stroke="currentColor" />
      <rect x="9" y="3" width="5.5" height="10" rx="1" fill="none" stroke="currentColor" />
      <path d="M7.4 8h1.2M7.9 6.9L9.1 8l-1.2 1.1" fill="none" stroke="currentColor" strokeWidth="1.2" />
    </svg>
  </span>;
}
