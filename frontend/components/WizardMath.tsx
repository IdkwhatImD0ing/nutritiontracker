/**
 * §7.4 goal wizard shared shapes. The formula itself lives server-side only
 * (backend/services/goals.py, POST /api/settings/goals/compute) — one copy,
 * so the client can never drift from the numbers that get saved.
 */

export type Sex = "male" | "female";
export type Activity =
  | "sedentary"
  | "light"
  | "moderate"
  | "active"
  | "very_active";

export type GoalInput = {
  sex: Sex;
  age: number;
  height_cm: number;
  weight_kg: number;
  activity: Activity;
  target_weight_kg: number;
  /** signed kg per week: negative = lose, 0 = maintain, positive = gain */
  weekly_rate_kg: number;
};

export type GoalResult = {
  bmr: number;
  tdee: number;
  targets: { kcal: number; protein_g: number; carb_g: number; fat_g: number };
  /** clamps the server applied (safe kcal floor, rate cap), for the UI */
  notes?: string[];
};

export const ACTIVITY: { id: Activity; label: string }[] = [
  { id: "sedentary", label: "Sedentary — desk job, little exercise" },
  { id: "light", label: "Light — 1–3 sessions a week" },
  { id: "moderate", label: "Moderate — 3–5 sessions a week" },
  { id: "active", label: "Active — 6–7 sessions a week" },
  { id: "very_active", label: "Very active — hard daily training" },
];

/** Shape check for the server's answer — a differently-shaped response is an
 *  error rather than something to render NaN from. */
export function isGoalResult(x: unknown): x is GoalResult {
  const r = x as GoalResult | null;
  const t = r?.targets;
  return (
    !!t &&
    typeof r.bmr === "number" &&
    typeof r.tdee === "number" &&
    typeof t.kcal === "number" &&
    typeof t.protein_g === "number" &&
    typeof t.carb_g === "number" &&
    typeof t.fat_g === "number"
  );
}

// unit helpers for the imperial inputs
export const LB_PER_KG = 2.2046226;
export const CM_PER_IN = 2.54;
