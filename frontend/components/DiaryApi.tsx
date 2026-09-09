"use client";

/**
 * Diary entry seams for the Today diary list + the History day view (§5
 * `/api/entries/:id` PATCH/DELETE, §3.4a citation on diary rows).
 *
 * `apiRequest` mirrors lib/api.ts's private `request()` (Bearer + one 401
 * retry) because that helper is not exported. The token cache the two used to
 * duplicate is now one module, lib/auth/token.ts; the request wrapper itself
 * is still a copy, which costs nothing but a few lines.
 */

import { ApiError, type DayTotals } from "@/lib/api";
import type { components } from "@/lib/api-types";
import { getToken } from "@/lib/auth/token";

export type WebSource = components["schemas"]["WebSource"];

/** §3.4a: the diary renders a web-transcribed food's citation as a visible,
 *  clickable link. routes/diary.py emits `web_source` {url, title}, `source`
 *  ('fdc' | 'off' | 'web') and `grams` on each entry (routes/entries.py's
 *  PATCH response carries `grams` but leaves the other two null), plus (M4)
 *  `estimated`: "portion" on a row whose amount was read off a photo and
 *  confirmed before commit — an "unresolved" row never commits. All
 *  generated from /openapi.json. */
export type DiaryEntry = components["schemas"]["DiaryEntry"];

export type DiaryMeals = Record<string, DiaryEntry[]>;

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

/** §3.5: a qty-only PATCH scales the stored snapshot (BACKLOG 28 base); a
 *  `serving_id` re-derives it from the foods cache and resets that base. */
export function patchEntry(
  entryId: string,
  patch: { qty?: number; serving_id?: string },
): Promise<EntryPatchResponse> {
  return apiRequest(`/api/entries/${entryId}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

/** DELETE /api/entries/:id → the day's updated totals. */
export function deleteEntry(entryId: string): Promise<DayTotals> {
  return apiRequest(`/api/entries/${entryId}`, { method: "DELETE" });
}
