"use client";

/**
 * Typed fetch wrapper over the generated api-types (§2 shared-types rule):
 * every request/response type below is derived from lib/api-types.ts, which
 * is generated from the backend's /openapi.json — never hand-maintained.
 *
 * Attaches Authorization: Bearer <JWT from authClient.token()> and retries
 * exactly once on 401 with a freshly fetched token.
 */

import type { VoiceClip } from "@/components/useVoiceCapture";
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
/** §8.4: POST /api/logs answers LogCreateResponse — a LogResponse plus the
 *  itemless "save this as my usual breakfast" outcome (`saved_meal`, or
 *  `note` saying why nothing was saved), and (§6.4) `agent`: what the agent
 *  lane did — its reply and the weigh-in it recorded, if any. All three are
 *  absent on an ordinary log. */
export type LogCreateResponse = LogResponse &
  Pick<
    components["schemas"]["LogCreateResponse"],
    "saved_meal" | "note" | "agent"
  >;
export type AgentOutcome = components["schemas"]["AgentOutcome"];
/** §6.4 weight tracking: one weigh-in (routes/weights.py) — kg plus the
 *  unit it was said in, which is how the card shows it back. */
export type WeightEntry = components["schemas"]["WeightEntry"];
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
  /** FastAPI's `detail` when the body was JSON with a string one — the
   *  server's own user-facing sentence. A demo-quota 429 also carries
   *  `resets_at` (ISO UTC); callers show `detail` verbatim (§13: never a
   *  generic error where the server already said why). */
  detail: string | null = null;
  resetsAt: string | null = null;
  constructor(status: number, body: string) {
    super(`API ${status}: ${body.slice(0, 200)}`);
    this.status = status;
    try {
      const j = JSON.parse(body);
      // {detail, resets_at} at the top level, or nested one deeper when the
      // route raised HTTPException(detail={...}).
      const d = j && typeof j.detail === "object" && j.detail ? j.detail : j;
      if (typeof d?.detail === "string") this.detail = d.detail;
      if (typeof d?.resets_at === "string") this.resetsAt = d.resets_at;
    } catch {
      /* plain-text body — nothing to surface beyond the status */
    }
  }
}

/** The demo quota's refusal (HTTP 429, services/quota.py) or null for any
 *  other failure: `detail` is the server's sentence ("Demo accounts can log
 *  once a day — resets at …"), shown verbatim in the lane's own error
 *  surface; `resetsAt` the ISO UTC instant the banner turns into local time. */
export type QuotaRefusal = { detail: string; resetsAt: string | null };

export function quotaRefusal(e: unknown): QuotaRefusal | null {
  if (!(e instanceof ApiError) || e.status !== 429) return null;
  return {
    detail: e.detail ?? "Demo limit reached — try again tomorrow.",
    resetsAt: e.resetsAt,
  };
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

export function createLog(body: LogCreate): Promise<LogCreateResponse> {
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

/** §6.4 weight tracking: GET /api/weights?days= — the newest weigh-in and
 *  the window's entries, newest first (routes/weights.py). */
export function getWeights(
  days = 90,
): Promise<Ok<operations["get_weights_api_weights_get"]>> {
  return request(`/api/weights?days=${days}`);
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

/** GET /api/foods/recent — the foods you have logged, newest first, one per
 *  food, capped server-side at 200: the Search sheet's "previously added"
 *  list (owner ruling, 2026-09-06). Every hit is already cached (food_id
 *  set), so a pick skips /api/foods/select. */
export function recentFoods(): Promise<FoodSearchResult[]> {
  return request("/api/foods/recent");
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

// ---------------------------------------------------------------------------
// M2 — voice path + the §7.1 edit sheet's alias list.
// ---------------------------------------------------------------------------

/** Same Bearer scheme + single 401 retry as request(), minus the JSON
 *  content-type: the browser sets multipart/form-data with its boundary. */
async function multipart(path: string, form: FormData): Promise<Response> {
  const attempt = async (forceToken: boolean) => {
    const token = await getToken(forceToken);
    const headers = new Headers();
    if (token) headers.set("authorization", `Bearer ${token}`);
    return fetch(path, { method: "POST", body: form, headers });
  };
  let res = await attempt(false);
  if (res.status === 401) res = await attempt(true);
  return res;
}

export type AudioLogResult =
  | { kind: "staged"; log: LogCreateResponse }
  /** §5/§13: the transcript came back empty — a capture failure, not an empty
   *  meal; the server has already recorded it on the audio log's row. */
  | { kind: "capture_failure" };

type CaptureFailure = components["schemas"]["CaptureFailureResponse"];

/** §5: POST /api/logs as multipart — the audio blob under `audio` plus the
 *  same fields as the JSON body minus `text` (the transcript supplies it). */
export async function createLogAudio(args: {
  id: string;
  clip: VoiceClip;
  tz: string;
  /** a client-detected failure from an earlier attempt that this clip is
   *  retrying — lands in logs.capture_error so the §9 counter still sees it
   *  (capture_fallback stays false: voice succeeded, nothing fell back) */
  capture_error?: string | null;
  /** §6.4: the Agent lane — the transcript runs through the front-door agent */
  agent?: boolean;
}): Promise<AudioLogResult> {
  const { blob, mimeType, capturedAt } = args.clip;
  const ext = mimeType.includes("mp4") ? "m4a" : "webm";
  const form = new FormData();
  form.set("id", args.id);
  form.set("captured_at", capturedAt);
  form.set("tz", args.tz);
  if (args.capture_error) form.set("capture_error", args.capture_error);
  if (args.agent) form.set("agent", "true");
  form.set("audio", new File([blob], `clip.${ext}`, { type: mimeType }));
  const res = await multipart("/api/logs", form);
  if (!res.ok) {
    throw new ApiError(res.status, await res.text().catch(() => ""));
  }
  const body = (await res.json()) as LogCreateResponse | CaptureFailure;
  if ("status" in body && body.status === "capture_failure") {
    return { kind: "capture_failure" };
  }
  return { kind: "staged", log: body as LogCreateResponse };
}

/** One row of the user's aliases for a phrase (§4 aliases ⋈ foods) — the
 *  minimal shape the §7.1 edit sheet renders. */
export type AliasRow = {
  id: string;
  food_id: string;
  food_name: string;
  brand?: string | null;
  /** absent when the row was derived from the staged payload's alternates
   *  rather than read from the aliases table */
  use_count?: number | null;
  pinned?: boolean | null;
};

/** §5 GET /api/aliases?phrase= (routes/aliases.py, M3): his alias rows for
 *  norm(phrase). FoodEditSheet still treats any failure as "no aliases" and
 *  falls back to the staged row's own food_id-bearing alternates. */
export async function getAliasesForPhrase(phrase: string): Promise<AliasRow[]> {
  const qs = new URLSearchParams({ phrase });
  const body = await request<{ aliases?: AliasRow[] }>(
    `/api/aliases?${qs.toString()}`,
  );
  return Array.isArray(body?.aliases) ? body.aliases : [];
}
