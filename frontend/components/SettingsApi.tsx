"use client";

/**
 * Settings + the goal wizard (§6.1 settings keys, §7.4 wizard) over
 * backend/routes/settings.py:
 *
 *   GET  /api/settings                → every user-facing key
 *   PUT  /api/settings                partial update (only keys present)
 *   POST /api/settings/goals/compute  wizard inputs → proposed targets
 *
 * The wizard's `Body` (GoalInput names: activity, target_weight_kg, a SIGNED
 * weekly_rate_kg) is translated to/from the route's `BodyProfile`
 * (activity_level, goal_weight_kg, rate as a magnitude — the direction is
 * the goal weight vs the current weight) here, so neither side renames.
 */

import { apiFetch } from "./LibraryApi";
import type { Activity, GoalInput, GoalResult, Sex } from "./WizardMath";

/** backend/routes/settings.py BodyProfile / GoalInputs field names. */
type ServerBody = {
  height_cm?: number | null;
  weight_kg?: number | null;
  age?: number | null;
  sex?: Sex | null;
  activity_level?: Activity | null;
  goal_weight_kg?: number | null;
  weekly_rate_kg?: number | null; // magnitude, 0..1 kg/wk
  units?: "metric" | "imperial" | null;
};

const isNum = (v: unknown): v is number =>
  typeof v === "number" && Number.isFinite(v);

function toServerBody(b: Body): ServerBody {
  const out: ServerBody = {};
  if (isNum(b.height_cm)) out.height_cm = b.height_cm;
  if (isNum(b.weight_kg)) out.weight_kg = b.weight_kg;
  if (isNum(b.age)) out.age = b.age;
  if (b.sex) out.sex = b.sex;
  if (b.activity) out.activity_level = b.activity;
  if (isNum(b.target_weight_kg)) out.goal_weight_kg = b.target_weight_kg;
  if (isNum(b.weekly_rate_kg)) out.weekly_rate_kg = Math.abs(b.weekly_rate_kg);
  if (b.units) out.units = b.units;
  return out;
}

function fromServerBody(raw: unknown): Body {
  const s = (raw && typeof raw === "object" ? raw : {}) as ServerBody;
  const out: Body = {};
  if (isNum(s.height_cm)) out.height_cm = s.height_cm;
  if (isNum(s.weight_kg)) out.weight_kg = s.weight_kg;
  if (isNum(s.age)) out.age = s.age;
  if (s.sex) out.sex = s.sex;
  if (s.activity_level) out.activity = s.activity_level;
  if (isNum(s.goal_weight_kg)) out.target_weight_kg = s.goal_weight_kg;
  if (isNum(s.weekly_rate_kg)) {
    const goal = s.goal_weight_kg;
    const now = s.weight_kg;
    const dir = isNum(goal) && isNum(now) && goal !== now ? Math.sign(goal - now) : 0;
    out.weekly_rate_kg = dir === 0 ? 0 : dir * s.weekly_rate_kg;
  }
  if (s.units === "metric" || s.units === "imperial") out.units = s.units;
  return out;
}

export type MealWindow = { start: string; end: string };

export type MealWindows = {
  breakfast: MealWindow;
  lunch: MealWindow;
  dinner: MealWindow;
  /** the meal outside every window (§7.2 default: snack) */
  default: string;
};

export type Targets = {
  kcal: number;
  protein_g: number;
  carb_g: number;
  fat_g: number;
};

/** The wizard's inputs, kept so Settings can re-run it pre-filled. */
export type Body = Partial<GoalInput> & { units?: "metric" | "imperial" };

export type Settings = {
  /** `{}` until the goal wizard has run (seeded empty, §4) */
  targets: Partial<Targets>;
  meal_windows: MealWindows;
  timezone: string;
  profile_notes: string;
  cronometer_baseline_days_per_week: number;
  body: Body;
};

export const DEFAULT_WINDOWS: MealWindows = {
  breakfast: { start: "04:00", end: "11:00" },
  lunch: { start: "11:00", end: "15:30" },
  dinner: { start: "15:30", end: "21:30" },
  default: "snack",
};

/** Tolerates a partially-seeded settings table: every key gets a default. */
function normalize(raw: Partial<Settings> | null | undefined): Settings {
  return {
    targets:
      raw?.targets && typeof raw.targets === "object" ? raw.targets : {},
    meal_windows: { ...DEFAULT_WINDOWS, ...(raw?.meal_windows ?? {}) },
    timezone: raw?.timezone || "America/Los_Angeles",
    profile_notes: raw?.profile_notes ?? "",
    cronometer_baseline_days_per_week:
      typeof raw?.cronometer_baseline_days_per_week === "number"
        ? raw.cronometer_baseline_days_per_week
        : 0,
    body: fromServerBody(raw?.body),
  };
}

export async function getSettings(): Promise<Settings> {
  return normalize(await apiFetch<Partial<Settings>>("/api/settings"));
}

export async function putSettings(
  patch: Partial<Settings>,
): Promise<Settings> {
  const { body, targets, ...rest } = patch;
  const wire: Record<string, unknown> = { ...rest };
  if (body) wire.body = toServerBody(body);
  // The route's TargetsIn wants all four numbers; `{}` (a Settings save
  // while no targets are set) has no server representation, so it is simply
  // not sent.
  if (targets && typeof targets.kcal === "number") wire.targets = targets;
  return normalize(
    await apiFetch<Partial<Settings>>("/api/settings", {
      method: "PUT",
      body: JSON.stringify(wire),
    }),
  );
}

/** §7.4: the wizard shows when settings.targets is empty after sign-in. */
export function targetsEmpty(s: Settings): boolean {
  const t = s.targets;
  return !(typeof t.kcal === "number" && t.kcal > 0);
}

/** POST /api/settings/goals/compute — the §7.4 formula server-side
 *  (services/goals.py). Nothing is saved; the wizard PUTs `targets` after. */
export function computeGoals(input: GoalInput): Promise<GoalResult> {
  return apiFetch("/api/settings/goals/compute", {
    method: "POST",
    body: JSON.stringify(toServerBody(input)),
  });
}
