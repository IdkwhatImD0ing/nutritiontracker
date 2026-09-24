"use client";

/**
 * Oura (backend routes/oura.py, services/oura.py; owner only). Shapes come
 * from the generated api-types (§2 shared-types rule), fetched through
 * LibraryApi's apiFetch (Bearer + one 401 retry) like the other seams.
 *
 *   GET  /api/oura           → { available, connected, last_synced_at, missing, days }
 *   GET  /api/oura/authorize → { url } — Connect and Reconnect send the browser there
 *   POST /api/oura/sync      → { skipped, days, workouts, last_synced_at, missing }
 *
 * `missing` names the scopes Oura refused ("daily", "workout").
 * The callback (/api/oura/callback) is Oura's browser redirect, not a fetch:
 * it lands back on /settings?oura=connected|missing_scope|denied|error|not_set_up.
 */

import type { components } from "@/lib/api-types";
import { apiFetch } from "./LibraryApi";

export type OuraStatus = components["schemas"]["OuraStatus"];
export type OuraSync = components["schemas"]["OuraSync"];
type OuraAuthorize = components["schemas"]["OuraAuthorize"];

export function getOuraStatus(): Promise<OuraStatus> {
  return apiFetch("/api/oura");
}

/** Leaves the app for Oura's consent screen. */
export async function connectOura(): Promise<void> {
  const { url } = await apiFetch<OuraAuthorize>("/api/oura/authorize");
  window.location.assign(url);
}

/** `force` skips the server's half-hour throttle (the card's Sync now). */
export function syncOura(force = false): Promise<OuraSync> {
  return apiFetch(`/api/oura/sync${force ? "?force=true" : ""}`, {
    method: "POST",
  });
}
