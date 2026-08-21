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

export type StagedItem = components["schemas"]["StagedItem"];
export type LogCreate = components["schemas"]["LogCreate"];
export type LogResponse = components["schemas"]["LogResponse"];
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
  return (await res.json()) as T;
}

export function createLog(
  body: LogCreate,
): Promise<Ok<operations["create_log_api_logs_post"]>> {
  return request("/api/logs", { method: "POST", body: JSON.stringify(body) });
}

export function getLog(
  logId: string,
): Promise<Ok<operations["get_log_api_logs__log_id__get"]>> {
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

export function resolveItem(
  body: ResolveRequest,
): Promise<Ok<operations["resolve_item_api_resolve_post"]>> {
  return request("/api/resolve", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function kickTier3(
  body: Tier3KickRequest,
): Promise<Ok<operations["kick_tier3_api_resolve_tier3_post"]>> {
  return request("/api/resolve/tier3", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function answerQuestion(
  body: AnswerRequest,
): Promise<Ok<operations["answer_question_api_resolve_answer_post"]>> {
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
