/* Where a food's numbers came from, in words a person reads. Mirrors the
   backend's routes/foods.py _SOURCE_LABELS (the long forms), plus "menu" —
   the photo lane's café-menu foods (routes/photo.py), which that map does
   not list yet. Plain TS. */

/** Short: a search result's sub-line, a row's badge. */
export const SOURCE_LABEL: Record<string, string> = {
  fdc: "USDA",
  off: "Open Food Facts",
  web: "Web",
  menu: "Café menu",
  estimate: "Photo estimate",
};

/** Long: food detail's Source card, where there is room to say it in full. */
export const SOURCE_LABEL_LONG: Record<string, string> = {
  fdc: "USDA FoodData Central",
  off: "Open Food Facts",
  web: "Web",
  menu: "Café menu",
  estimate: "Photo estimate",
};

/** A source code as words; an unknown code comes back as given. */
export function sourceName(source: string, long = false): string {
  return (long ? SOURCE_LABEL_LONG : SOURCE_LABEL)[source] ?? source;
}
