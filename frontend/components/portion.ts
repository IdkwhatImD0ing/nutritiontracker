/* Portion arithmetic shared by the three portion editors: the diary row,
   the staging sheet's portion sheet and the saved-meal sheet. Plain TS. */

/** The same amount of food counted in another serving: qty × from ÷ to, to
 *  2 dp (floored at 0.01, so a tiny amount is still a valid draft). null when
 *  either serving's weight is unknown — the caller keeps the number it has
 *  rather than guess. Switching "0.5 × 1 cup (148 g)" to "g" gives 74, not
 *  0.5 g. */
export function rescaleQty(
  qty: number,
  fromGrams: number | null | undefined,
  toGrams: number | null | undefined,
): number | null {
  if (!fromGrams || !toGrams) return null;
  return Math.max(0.01, Math.round(((qty * fromGrams) / toGrams) * 100) / 100);
}

/** A serving that is one gram ("g", "1 g"): its qty is the weight itself. */
export function isGramServing(grams: number | null | undefined): boolean {
  return grams != null && Math.abs(grams - 1) < 0.05;
}

/** One tap of a −/+ stepper. A gram serving steps by 10 with a floor of 1 —
 *  a gram a tap is no step at all; any other serving steps by the editor's
 *  own `delta` with the 0.25 floor all three editors use. */
export function stepQty(
  qty: number,
  dir: 1 | -1,
  servingGrams: number | null | undefined,
  delta: number,
): number {
  const grams = isGramServing(servingGrams);
  const next = Math.round((qty + dir * (grams ? 10 : delta)) * 100) / 100;
  return Math.max(grams ? 1 : 0.25, next);
}

/** A qty as a field's text: up to 2 dp and no thousands separator, because
 *  the field is read back with Number() and Number("1,200") is NaN. */
export function qtyInput(qty: number): string {
  return String(Math.round(qty * 100) / 100);
}
