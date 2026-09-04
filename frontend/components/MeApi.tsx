"use client";

/**
 * GET /api/me — whose JWT this is and what the demo quota says
 * (backend routes/me.py, services/quota.py). Shapes come from the generated
 * api-types (§2 shared-types rule), fetched through LibraryApi's apiFetch
 * (Bearer + one 401 retry) like the other seams.
 *
 *   GET /api/me → { email, is_owner, demo: null | { limit, used_today, resets_at } }
 *
 * `demo` is null for the owner (unlimited). For every other account it is
 * the per-UTC-day log quota across all lanes: `used_today` counts each
 * attempt (capture failures included), `resets_at` is the next UTC
 * midnight as ISO. Over quota, the log routes answer 429 with the same
 * sentence lib/api.ts exposes as ApiError.detail.
 */

import type { components } from "@/lib/api-types";
import { apiFetch } from "./LibraryApi";

export type DemoQuota = components["schemas"]["DemoQuota"];
export type Me = components["schemas"]["MeResponse"];

export function getMe(): Promise<Me> {
  return apiFetch("/api/me");
}
