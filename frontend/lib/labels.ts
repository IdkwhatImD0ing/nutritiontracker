/* How the app names its own things on screen: meals and foods. Plain TS. */

import type { components } from "./api-types";

type Meal = components["schemas"]["Meal"];

/** The four meals as the UI writes them — one form everywhere, where the
 *  value itself stays lowercase on the wire. */
export const MEAL_LABEL: Record<Meal, string> = {
  breakfast: "Breakfast",
  lunch: "Lunch",
  dinner: "Dinner",
  snack: "Snack",
};

/** MEAL_LABEL for any string a screen holds (Settings keeps meals as plain
 *  strings); an unknown one comes back with its first letter raised. */
export function mealLabel(meal: string): string {
  return MEAL_LABEL[meal as Meal] ?? meal.charAt(0).toUpperCase() + meal.slice(1);
}

/** A food's name in sentence case, everywhere (the owner's call on issue
 *  #149, replacing globals.css `.food-name`'s capitalize): the first
 *  letter raised and the rest as stored, so "fairlife 2% reduced fat milk"
 *  reads "Fairlife 2% reduced fat milk" — not CSS capitalize's "Reduced Fat
 *  Milk". A provider's ALL-CAPS description ("GREEK YOGURT, PLAIN") is
 *  lowered first, or it would still shout. */
export function foodName(name: string): string {
  const allCaps = name === name.toUpperCase() && name !== name.toLowerCase();
  const s = allCaps ? name.toLowerCase() : name;
  return s.charAt(0).toUpperCase() + s.slice(1);
}
