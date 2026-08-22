"use client";

/**
 * Diary entry seams for the Today diary list + the History day view (§5
 * `/api/entries/:id` PATCH/DELETE, §3.4a citation on diary rows).
 *
 * `apiRequest` mirrors lib/api.ts's private `request()` (Bearer from
 * authClient.token(), one 401 retry) because that helper is not exported and
 * lib/api.ts is outside this workstream; the integrator can collapse the two
 * by exporting `request` from lib/api.ts and deleting the copy below.
 */

import { ApiError, type DayTotals } from "@/lib/api";
import type { components } from "@/lib/api-types";
import { authClient } from "@/lib/auth/client";

export type WebSource = components["schemas"]["WebSource"];

/** §3.4a: the diary renders a web-transcribed food's citation as a visible,
 *  clickable link. routes/diary.py now emits `web_source` {url, title},
 *  `source` ('fdc' | 'off' | 'web') and `grams` on each entry; they are
 *  typed here as optional until typegen reruns on the updated /openapi.json
 *  (routes/entries.py's PATCH response leaves them null). */
export type DiaryEntry = components["schemas"]["DiaryEntry"] & {
  web_source?: WebSource | null;
  source?: string | null;
  grams?: number | null;
};

export type DiaryMeals = Record<string, DiaryEntry[]>;

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

export async function apiRequest<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const attempt = async (forceToken: boolean) => {
    const token = await getToken(forceToken);
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

export type EntryPatchResponse = {
  entry: DiaryEntry;
  totals: DayTotals;
};

/** §3.5: a qty-only PATCH scales the stored snapshot (BACKLOG 28 base). */
export function patchEntryQty(
  entryId: string,
  qty: number,
): Promise<EntryPatchResponse> {
  return apiRequest(`/api/entries/${entryId}`, {
    method: "PATCH",
    body: JSON.stringify({ qty }),
  });
}

/** DELETE /api/entries/:id → the day's updated totals. */
export function deleteEntry(entryId: string): Promise<DayTotals> {
  return apiRequest(`/api/entries/${entryId}`, { method: "DELETE" });
}
