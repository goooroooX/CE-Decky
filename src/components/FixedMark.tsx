import type { TableDerivation } from "../types";

/**
 * One green chip on a row whose bytes are a fixed copy of another table.
 *
 * The same chip as `SignedMark`, and for the same reason a word: provenance is
 * a property of the bytes, known before anything is tried, and the panel calls
 * these tables **Fixed** wherever it names where a table came from. Green
 * because a fixed copy is the answer to a table that did not work here, and
 * a word rather than a glyph so it does not read as the compatibility verdict
 * drawn beside it in a similar green.
 *
 * The title says whose change it was: a copy CE Decky made itself, or one
 * repaired outside it and imported with its source named.
 */
export function FixedMark({ derivation }: { derivation: Pick<TableDerivation, "transforms"> | null | undefined }) {
  const outside = derivation?.transforms?.includes("external-repair") === true;
  return <span
    title={outside ? "Repaired outside CE Decky from another table" : "CE Decky made this copy from another table"}
    style={{
      flex: "0 0 auto",
      padding: "0 4px",
      borderRadius: 3,
      fontSize: "0.7em",
      lineHeight: "16px",
      fontWeight: 700,
      whiteSpace: "nowrap",
      background: "hsla(105, 60%, 66%, 0.85)",
      color: "hsla(0, 0%, 0%, 0.86)",
    }}
  >Fixed</span>;
}
