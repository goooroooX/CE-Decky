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
 * It sits on the same sixteen pixel line the glyphs beside it are tall, and it
 * is the narrowest chip in the product on purpose. Wherever it is drawn it
 * stands beside something the reader came to read - a table's name on Manage,
 * a result's title on Search - and on a Steam Deck that line is 854 pixels
 * wide for everything on it. Drawn the way the search rows draw their own
 * chips, upper case and letter-spaced, it measured 61 pixels against the 45 it
 * measures here: sixteen pixels of a name, bought with nothing but shouting.
 * So it keeps the word and gives up the case, the spacing between its letters
 * and two pixels of padding each side.
 *
 * The word is the name and the sentence is a description of it, which is what
 * `title` carries. It takes no `aria-label`: that would replace the word a
 * reader can see with a sentence they cannot, which is what the product's
 * wordless marks use one for and this one has no need of.
 */
export function SignedMark() {
  return <span
    title={SIGNED_LABEL}
    style={{
      flex: "0 0 auto",
      padding: "0 4px",
      borderRadius: 3,
      fontSize: "0.7em",
      lineHeight: "16px",
      fontWeight: 700,
      whiteSpace: "nowrap",
      background: "hsla(41, 73%, 65%, 0.85)",
      color: "hsla(0, 0%, 0%, 0.86)",
    }}
  >Signed</span>;
}
