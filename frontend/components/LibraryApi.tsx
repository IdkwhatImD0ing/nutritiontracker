"use client";

/**
 * M3 seams — the §5 routes the Library / food-detail surfaces consume. None
 * of them existed in openapi.json when this workstream was built, so the
 * shapes below are the route-local contracts this client expects (the
 * backend's pydantic twins live in their own route files; models.py is
 * untouched). Integrator: once the routes land in openapi.json, swap these
 * hand-typed shapes for components["schemas"][...] and `apiFetch` for
 * lib/api.ts's `request` (same Bearer scheme; not exported today).
 *
 *   GET    /api/aliases[?phrase=]      → { aliases: AliasRow[] }
 *   PATCH  /api/aliases/:id            { pinned?, default_serving_id?, default_qty? } → AliasRow
 *   DELETE /api/aliases/:id            → 204
 *   GET    /api/foods/:id              → FoodDetail (foods row + its aliases)
 *   POST   /api/foods/select           (existing; library path, no log context) → { food_id }
 *   GET    /api/meals?include_inactive=1 → { meals: SavedMeal[] }
 *   POST   /api/meals                  { name, trigger_phrases, items } → SavedMeal (201)
 *   PATCH  /api/meals/:id              { name?, trigger_phrases?, items?, is_active? } → SavedMeal
 */

import {
  ApiError,
  type FoodSearchResult,
  type StagedServing,
} from "@/lib/api";
import { authClient } from "@/lib/auth/client";

// ---------------------------------------------------------------------------
// fetch — Bearer <JWT> + one retry on 401 (mirrors lib/api.ts request())
// ---------------------------------------------------------------------------

let cached: { token: string; exp: number } | null = null;

function jwtExp(token: string): number {
  try {
    const payload = JSON.parse(atob(token.split(".")[1] ?? ""));
    return typeof payload.exp === "number" ? payload.exp * 1000 : 0;
  } catch {
    return 0;
  }
}

async function getToken(force: boolean): Promise<string | null> {
  if (!force && cached && cached.exp - Date.now() > 30_000) return cached.token;
  const res = await authClient.token();
  const token = res.data?.token ?? null;
  cached = token ? { token, exp: jwtExp(token) } : null;
  return token;
}

export async function apiFetch<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const attempt = async (force: boolean) => {
    const token = await getToken(force);
    const headers = new Headers(init.headers);
    headers.set("content-type", "application/json");
    if (token) headers.set("authorization", `Bearer ${token}`);
    return fetch(path, { ...init, headers });
  };
  let res = await attempt(false);
  if (res.status === 401) res = await attempt(true);
  if (!res.ok) throw new ApiError(res.status, await res.text().catch(() => ""));
  const text = await res.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

// ---------------------------------------------------------------------------
// aliases (§4 aliases ⋈ foods; §7.1 edit sheet: pin, default portion, delete)
// ---------------------------------------------------------------------------

export type AliasRow = {
  id: string;
  alias_norm: string;
  food_id: string;
  food_name: string;
  brand?: string | null;
  default_serving_id?: string | null;
  default_qty?: number | null;
  state_answer?: string | null;
  pinned: boolean;
  use_count: number;
  last_used_at?: string | null;
  rejected_count?: number | null;
  origin?: string | null;
};

export type AliasPatch = {
  pinned?: boolean;
  default_serving_id?: string | null;
  default_qty?: number | null;
};

export async function getAliases(phrase?: string): Promise<AliasRow[]> {
  const qs = phrase ? `?${new URLSearchParams({ phrase })}` : "";
  const body = await apiFetch<{ aliases?: AliasRow[] }>(`/api/aliases${qs}`);
  return Array.isArray(body?.aliases) ? body.aliases : [];
}

export function patchAlias(id: string, patch: AliasPatch): Promise<AliasRow> {
  return apiFetch(`/api/aliases/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

export function deleteAlias(id: string): Promise<void> {
  return apiFetch(`/api/aliases/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

// ---------------------------------------------------------------------------
// food detail (§7.1 "everything the database returned" + §7.4 drill-in)
// ---------------------------------------------------------------------------

/** routes/foods.py NutrientRow — one nutrient exactly as the source reported
 *  it (§7.1 "everything the database returned"). `basis` is the denominator:
 *  "per 100 g" / "per serving (label)" / "as reported". */
export type NutrientRow = {
  name: string;
  amount: number;
  unit?: string | null;
  basis: string;
};

export type FoodDetail = {
  id: string;
  source: string; // 'fdc' | 'off' | 'web'
  source_id: string;
  source_url?: string | null;
  provenance?: {
    citations?: { url: string; title?: string | null }[] | null;
    page_title?: string | null;
    [k: string]: unknown;
  } | null;
  name: string;
  display_name: string;
  brand?: string | null;
  food_type: string;
  state?: string | null;
  state_sensitive?: boolean | null;
  servings: StagedServing[];
  default_serving_id: string;
  per_100g?: Record<string, number> | null;
  /** every nutrient row the source carried, un-normalized */
  nutrients?: NutrientRow[] | null;
  /** the raw provider response, verbatim (foods.payload) */
  payload?: unknown;
  created_at?: string | null;
  refreshed_at?: string | null;
  /** his phrases pointing at this food, with use counts */
  aliases?: AliasRow[] | null;
};

export function getFood(id: string): Promise<FoodDetail> {
  return apiFetch(`/api/foods/${encodeURIComponent(id)}`);
}

/** Library manual search → cache an FDC hit (POST /api/foods/select without
 *  log context — the existing route's library path) and return foods.id. */
export async function cacheFood(hit: FoodSearchResult): Promise<string> {
  if (hit.food_id) return hit.food_id;
  const res = await apiFetch<{ food_id?: string | null }>("/api/foods/select", {
    method: "POST",
    body: JSON.stringify({
      source: hit.source,
      source_id: hit.source_id,
      display_name: hit.label,
    }),
  });
  if (!res.food_id) throw new ApiError(502, "select returned no food_id");
  return res.food_id;
}

// ---------------------------------------------------------------------------
// saved meals (§8.4: name + trigger phrases + fixed items; delete = deactivate)
// ---------------------------------------------------------------------------

export type SavedMealItem = {
  food_id: string;
  serving_id: string;
  qty: number;
  /** display joins the server adds (foods ⋈ servings); absent on a bare row */
  food_name?: string | null;
  brand?: string | null;
  serving_description?: string | null;
};

export type SavedMeal = {
  id: string;
  name: string;
  trigger_phrases: string[];
  items: SavedMealItem[];
  is_active: boolean;
  created_at?: string | null;
  updated_at?: string | null;
};

export type MealCreate = {
  name: string;
  /** empty → the server stores norm(name) as the one trigger */
  trigger_phrases: string[];
  items: { food_id: string; serving_id: string; qty: number }[];
};

export type MealPatch = {
  name?: string;
  trigger_phrases?: string[];
  items?: { food_id: string; serving_id: string; qty: number }[];
  is_active?: boolean;
};

/** GET /api/meals?include_inactive=1 → SavedMeal[] (routes/meals.py returns
 *  the list bare, active first). */
export async function getMeals(): Promise<SavedMeal[]> {
  const body = await apiFetch<SavedMeal[] | { meals?: SavedMeal[] }>(
    "/api/meals?include_inactive=1",
  );
  if (Array.isArray(body)) return body;
  return Array.isArray(body?.meals) ? body.meals : [];
}

/** POST /api/meals — §8.4 "tap Save as meal on any meal group" and "build one
 *  in Library". 409 when an active meal already carries that name; 422 when an
 *  item's food/serving isn't in the cache. */
export function createMeal(body: MealCreate): Promise<SavedMeal> {
  return apiFetch("/api/meals", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function patchMeal(id: string, patch: MealPatch): Promise<SavedMeal> {
  return apiFetch(`/api/meals/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}
