/** Spread on a field that takes a food in words (the searches, "+ add
 *  item", "Describe it differently"): iOS would otherwise autocorrect and
 *  capitalise brand and food words — "fage", "fairlife", "quest". Each field
 *  adds its own enterKeyHint ("search", or "go" where it resolves). */
export const FOOD_FIELD_PROPS = {
  autoCorrect: "off",
  autoCapitalize: "none",
  spellCheck: false,
} as const;
