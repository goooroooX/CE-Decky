const SAME_TABLE_LABEL = "Same table bytes as another source";

/**
 * One passive glyph for a result whose exact final bytes are already known from
 * a different origin.
 *
 * Provenance, never status: it is drawn in the panel's own accent rather than
 * in any compatibility colour, so it cannot be read as a second verdict beside
 * the green, amber, grey or red mark it sits next to. The word chip this
 * replaces made a row carrying `Same table`, `Local` and a compatibility
 * statement read like three separate findings about one table.
 *
 * The identity behind it is exact-final-CT-SHA equality with a distinct known
 * origin, decided before this is rendered. Nothing here infers a duplicate from
 * a filename, a URL, a provider row or an archive digest.
 */
export function SameTableMark() {
  return <span
    role="img"
    title={SAME_TABLE_LABEL}
    aria-label={SAME_TABLE_LABEL}
    style={{ color: "var(--ce-accent, hsla(203, 89%, 66%, 0.85))", whiteSpace: "nowrap", flexShrink: 0, lineHeight: 0 }}
  >
    <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true" focusable="false" style={{ verticalAlign: "middle" }}>
      {/* Two sheets of the same bytes, the one behind offset out from under the
          one in front, which is what "this is already here, from somewhere
          else" looks like without a word for it. */}
      <rect x="2.5" y="2.5" width="8" height="9.5" rx="1" fill="none" stroke="currentColor" />
      <rect x="5.5" y="4.5" width="8" height="9.5" rx="1" fill="none" stroke="currentColor" />
    </svg>
  </span>;
}
