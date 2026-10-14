/* Numbers, units, portions and dates, written one way across the app. Plain
   TS, no React, so a server page and a client row can both use it. */

/** en-US throughout: the app's copy is English, and a figure must not change
 *  its separators with the phone's region. */
export const fmt0 = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
export const fmt1 = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });
export const fmt2 = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });

const NBSP = " ";

/** "120 g", "2,854 kcal": a value and its unit joined by a no-break space,
 *  so a wrapping line never leaves the unit alone at the start of the next.
 *  A number goes through fmt1; pass a string to pick the precision
 *  (`withUnit(fmt0.format(kcal), "kcal")`). An empty unit is just the value. */
export function withUnit(n: number | string, unit: string): string {
  const v = typeof n === "number" ? fmt1.format(n) : n;
  return unit ? `${v}${NBSP}${unit}` : v;
}

/** The macro keys have names of their own ("carb_g" is "Carbs", not "Carb");
 *  every other key is read off its spelling. */
const CORE_PARTS: Record<string, { name: string; unit: string }> = {
  kcal: { name: "Calories", unit: "kcal" },
  protein_g: { name: "Protein", unit: "g" },
  carb_g: { name: "Carbs", unit: "g" },
  fat_g: { name: "Fat", unit: "g" },
};
const UNITS: Record<string, string> = { g: "g", mg: "mg", mcg: "mcg", ug: "mcg", iu: "IU" };

/** A macros key as a label and a unit: "vitamin_c_mg" → {name: "Vitamin C",
 *  unit: "mg"}, "vitamin_d_ug" → {…, unit: "mcg"}, "carb_g" → {name: "Carbs",
 *  unit: "g"}, for the micro lines in StagedRow and DiaryEntryRow and the
 *  nutrient lists in FoodDetail: the name is each word capitalised with the
 *  unit suffix dropped, and the unit is FoodDetail's (µg as "mcg", "IU"). A
 *  key with no known unit suffix has unit "". Render as `name` then
 *  `withUnit(value, unit)`. */
export function nutrientParts(key: string): { name: string; unit: string } {
  const core = CORE_PARTS[key];
  if (core) return core;
  const parts = key.split("_");
  const suffix = parts[parts.length - 1]?.toLowerCase() ?? "";
  const unit = UNITS[suffix] ?? "";
  if (unit) parts.pop();
  const name = parts.map((p) => p.charAt(0).toUpperCase() + p.slice(1)).join(" ");
  return { name, unit };
}

/** A serving that is grams and nothing else: "g", "100 g", "100g". */
const GRAM_SERVING = /^(\d+(?:\.\d+)?)?\s*g$/i;
/** The per-serving weight the adapters append: "1 cup (148 g)". */
const STATED_GRAMS = /\s*\((\d+(?:\.\d+)?)\s*g\)$/i;

/** True when the description already says how many grams it is ("100 g",
 *  "1 cup (148 g)"), so a grams column beside it would repeat it. */
export function statesGrams(description: string): boolean {
  return GRAM_SERVING.test(description.trim()) || STATED_GRAMS.test(description);
}

/** A portion as eaten. qty 1 (or unknown) is the serving as written. Any
 *  other qty keeps "qty × serving" — folding qty into free-text serving
 *  names ("½ cup") misreads "1 mug (12 fl oz)" — and names the grams eaten,
 *  not the per-serving weight in brackets:
 *
 *    (1,   "1 cup (148 g)")      → "1 cup (148 g)"
 *    (0.5, "1 cup (148 g)")      → "0.5 × 1 cup · 74 g"
 *    (2,   "1 large", 122)       → "2 × 1 large · 122 g"
 *    (150, "g")                  → "150 g"
 *    (1.5, "1 container")        → "1.5 × 1 container"
 *
 *  `totalGrams` (DiaryEntry.grams; serving grams × qty on a staged row) wins
 *  over grams read off the description. The space before "g" is a no-break
 *  space (withUnit). */
export function portionText(
  qty: number | null | undefined,
  description: string,
  totalGrams?: number | null,
): string {
  if (qty == null || qty === 1) return description;
  const gramServing = GRAM_SERVING.exec(description.trim());
  if (gramServing) {
    const grams = totalGrams ?? qty * Number(gramServing[1] ?? 1);
    return withUnit(fmt0.format(grams), "g");
  }
  const stated = STATED_GRAMS.exec(description);
  const grams = totalGrams ?? (stated ? qty * Number(stated[1]) : null);
  const serving = stated ? description.slice(0, stated.index) : description;
  const head = `${fmt2.format(qty)} × ${serving}`;
  return grams != null ? `${head} · ${withUnit(fmt0.format(grams), "g")}` : head;
}

/** A calendar date the way the app says it: "Apr 18, 2024", or "Apr 18" in
 *  the current year. Reads "2024-04-18" (as that local day, not UTC
 *  midnight, which is the day before in the Americas), FDC's "4/18/2024" and
 *  ISO timestamps; anything else comes back as given. */
export function fmtDate(input: string): string {
  const s = input.trim();
  let d: Date | null = null;
  let m = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(s);
  if (m) d = localDay(+m[1], +m[2], +m[3]);
  else if ((m = /^(\d{1,2})\/(\d{1,2})\/(\d{4})$/.exec(s))) d = localDay(+m[3], +m[1], +m[2]);
  else if (/^\d{4}-\d{2}-\d{2}T/.test(s)) d = new Date(s);
  if (!d || Number.isNaN(d.getTime())) return input;
  const thisYear = d.getFullYear() === new Date().getFullYear();
  return d.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    ...(thisYear ? {} : { year: "numeric" }),
  });
}

/** null for a day the month doesn't have ("2/31/2024"), which Date would
 *  silently roll into March. */
function localDay(y: number, month: number, day: number): Date | null {
  const d = new Date(y, month - 1, day);
  return d.getMonth() === month - 1 && d.getDate() === day ? d : null;
}
