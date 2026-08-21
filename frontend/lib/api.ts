"use client";

/**
 * Typed fetch wrapper over the generated api-types (§2 shared-types rule):
 * every request/response type below is derived from lib/api-types.ts, which
 * is generated from the backend's /openapi.json — never hand-maintained.
 *
 * Attaches Authorization: Bearer <JWT from authClient.token()> and retries
 * exactly once on 401 with a freshly fetched token.
 */

import type { components, operations } from "@/lib/api-types";
import { authClient } from "@/lib/auth/client";

/**
 * StagedItemOut = StagedItem + the GET-poll watchdog's optional `stale` flag
 * (§5) — a plain StagedItem is structurally assignable to it, so one type
 * serves every endpoint. `servings` (§7.1 portion sheet) is generated too.
 */
export type StagedServing = components["schemas"]["ServingOption"];
export type StagedItem = components["schemas"]["StagedItemOut"];
export type LogCreate = components["schemas"]["LogCreate"];
export type LogResponse = components["schemas"]["LogPollResponse"];
export type DayTotals = components["schemas"]["DayTotals"];
export type Macros = components["schemas"]["Macros"];
export type Meal = components["schemas"]["Meal"];
export type ResolveRequest = components["schemas"]["ResolveRequest"];
export type Tier3KickRequest = components["schemas"]["Tier3KickRequest"];
export type AnswerRequest = components["schemas"]["AnswerRequest"];

type JsonOf<T> = T extends { content: { "application/json": infer B } }
  ? B
  : never;
type Ok<Op> = Op extends { responses: { 200: infer R } } ? JsonOf<R> : never;

export class ApiError extends Error {
  status: number;
  constructor(status: number, body: string) {
    super(`API ${status}: ${body.slice(0, 200)}`);
    this.status = status;
  }
}

/** Small token cache so the hot path doesn't round-trip /token per request. */
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
  if (!force && cached && cached.exp - Date.now() > 30_000) {
    return cached.token;
  }
  const res = await authClient.token();
  const token = res.data?.token ?? null;
  cached = token ? { token, exp: jwtExp(token) } : null;
  return token;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const attempt = async (forceToken: boolean) => {
    const token = await getToken(forceToken);
    const headers = new Headers(init.headers);
    headers.set("content-type", "application/json");
    if (token) headers.set("authorization", `Bearer ${token}`);
    return fetch(path, { ...init, headers });
  };
  let res = await attempt(false);
  if (res.status === 401) {
    res = await attempt(true); // refresh once, then give up
  }
  if (!res.ok) {
    throw new ApiError(res.status, await res.text().catch(() => ""));
  }
  const text = await res.text();
  // 204 / empty bodies (DELETE, PATCH acks) parse to undefined, not a throw.
  return (text ? JSON.parse(text) : undefined) as T;
}

export function createLog(body: LogCreate): Promise<LogResponse> {
  return request("/api/logs", { method: "POST", body: JSON.stringify(body) });
}

export function getLog(logId: string): Promise<LogResponse> {
  return request(`/api/logs/${logId}`);
}

export function commitLog(
  logId: string,
  items: StagedItem[],
): Promise<Ok<operations["commit_log_api_logs__log_id__commit_post"]>> {
  return request(`/api/logs/${logId}/commit`, {
    method: "POST",
    body: JSON.stringify({ items }),
  });
}

export function resolveItem(body: ResolveRequest): Promise<StagedItem> {
  return request("/api/resolve", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function kickTier3(body: Tier3KickRequest): Promise<StagedItem> {
  return request("/api/resolve/tier3", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function answerQuestion(body: AnswerRequest): Promise<StagedItem> {
  return request("/api/resolve/answer", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function getDiary(
  date?: string,
): Promise<Ok<operations["get_diary_api_diary_get"]>> {
  const qs = date ? `?date=${encodeURIComponent(date)}` : "";
  return request(`/api/diary${qs}`);
}

/** §3.5 late-straggler append: POST /api/logs/:id/commit-item. */
export function commitItem(
  logId: string,
  item: StagedItem,
): Promise<Ok<operations["commit_item_api_logs__log_id__commit_item_post"]>> {
  return request(`/api/logs/${logId}/commit-item`, {
    method: "POST",
    body: JSON.stringify({ item_index: item.item_index, item }),
  });
}

// ---------------------------------------------------------------------------
// M1 seams — routes other Phase-2 workstreams add server-side (DESIGN.md §5).
// Shapes are hand-written against §5 + backend/models.py because the
// generated api-types.ts predates them; the integrator swaps each to
// `operations[...]` once typegen reruns. Callers treat failures of the
// telemetry-only calls (timings, remove) as non-fatal.
// ---------------------------------------------------------------------------

/** BACKLOG 2: PATCH /api/logs/:id/timings {staged_ms} — client-observed
 *  capture→rendered timing; there is no other write path for it (§5, §9). */
export function patchTimings(logId: string, stagedMs: number): Promise<void> {
  return request(`/api/logs/${logId}/timings`, {
    method: "PATCH",
    body: JSON.stringify({ staged_ms: Math.round(stagedMs) }),
  });
}

/** BACKLOG 11: DELETE /api/logs/:id — discard a staged log (records
 *  status:'discarded'; adherence-relevant data, §5). */
export function discardLog(logId: string): Promise<void> {
  return request(`/api/logs/${logId}`, { method: "DELETE" });
}

/** §7.1 remove-row telemetry: a removed row should write
 *  item_resolutions.status='removed' immediately, not only at commit (commit
 *  already treats absent rows as removed — that stays the backstop, so a
 *  failure here is swallowed by callers). Route path is the W3 seam. */
export function removeStagedItem(
  logId: string,
  itemIndex: number,
): Promise<void> {
  return request(`/api/logs/${logId}/items/${itemIndex}`, {
    method: "DELETE",
  });
}

/** BACKLOG 3: GET /api/foods/search?q=&source= — the boring escape hatch
 *  (local cache first, then FDC passthrough, §5). */
export type FoodSearchResult = components["schemas"]["FoodHit"];

export type FoodSearch = {
  hits: FoodSearchResult[]; // local first, then FDC generic, then branded
  /** exception class name when the FDC passthrough failed — local hits
   *  still render (the escape hatch must not 502) */
  fdcError: string | null;
};

export async function searchFoods(
  q: string,
  source?: "all" | "local" | "fdc",
): Promise<FoodSearch> {
  const qs = new URLSearchParams({ q });
  if (source) qs.set("source", source);
  const res = await request<components["schemas"]["FoodSearchResponse"]>(
    `/api/foods/search?${qs.toString()}`,
  );
  return {
    hits: [...res.local, ...res.generic, ...res.branded],
    fdcError: res.fdc_error ?? null,
  };
}

/** BACKLOG 3: resolve a not_found row to a manually searched food, recorded
 *  in item_resolutions like any other resolution. Local-cache hits (food_id
 *  set) go through /api/resolve/answer — /api/foods/select only accepts fdc
 *  ids (W4 contract); both paths are the same one-lookup, no-agent-run
 *  semantics (§5). */
export async function selectFood(args: {
  log_id: string;
  item_index: number;
  hit: FoodSearchResult;
}): Promise<StagedItem> {
  if (args.hit.food_id) {
    return answerQuestion({
      log_id: args.log_id,
      item_index: args.item_index,
      answer: { label: args.hit.label, food_id: args.hit.food_id },
    });
  }
  const res = await request<components["schemas"]["SelectResponse"]>(
    "/api/foods/select", {
    method: "POST",
    body: JSON.stringify({
      source: args.hit.source,
      source_id: args.hit.source_id,
      display_name: args.hit.label,
      log_id: args.log_id,
      item_index: args.item_index,
    }),
  });
  if (res.item == null) {
    throw new ApiError(502, "select returned no staged item");
  }
  return res.item;
}
