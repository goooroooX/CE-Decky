const SIGNED_LABEL = "Signed table; Cheat Engine usually refuses one";

/**
 * One amber chip on a row whose bytes carry the table's own `<Signature>`.
 *
 * Not a compatibility glyph, and deliberately a different shape from one.
 * `CompatibilityMark` is a verdict about whether the table worked here, earned
 * by running it; this is a property of the bytes, known before anything is
 * tried. Drawn as amber on the same ring it would have read as `retest`, which
 * means the opposite: it worked, and the build moved.
 *
 * Amber because it is worth stopping on - every signed table this project has
 * put in front of this Cheat Engine was refused - and a word because there is
 * no glyph a reader would read as "signed" without being told.
 *
 * Its metrics are the ones the search rows' own chips use, down to the sixteen
 * pixel line that the glyphs beside it are tall, so a row carrying this and a
 * `Local` chip reads as two chips rather than as two different kinds of object.
 */
export function SignedMark() {
  return <span
    title={SIGNED_LABEL}
    aria-label={SIGNED_LABEL}
    style={{
      flex: "0 0 auto",
      padding: "0 6px",
      borderRadius: 3,
      fontSize: "0.8em",
      lineHeight: "16px",
      fontWeight: 700,
      letterSpacing: "0.3px",
      textTransform: "uppercase",
      whiteSpace: "nowrap",
      background: "hsla(41, 73%, 65%, 0.85)",
      color: "hsla(0, 0%, 0%, 0.86)",
    }}
  >Signed</span>;
}
