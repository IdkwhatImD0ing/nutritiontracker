"use client";

/**
 * /api/keys — personal API keys for scripts, agents and MCP clients (backend
 * routes/keys.py, migration 006). Shapes come from the generated api-types,
 * fetched through LibraryApi's apiFetch (Bearer + one 401 retry) like the
 * other Settings seams.
 *
 *   GET    /api/keys      → ApiKeyOut[] newest first, revoked ones included
 *   POST   /api/keys      { label, scopes: "read" | "read_write",
 *                           expires_in_days: 30 | 90 | 365 | null }
 *                         → { key, secret } (201; 409 past 10 active keys)
 *   DELETE /api/keys/:id  → ApiKeyOut, now revoked (idempotent)
 *
 * `secret` is the whole key and exists only in that one POST response: the
 * server keeps its SHA-256, so the reveal banner is the only chance to copy
 * it. The routes answer only a signed-in session (a key cannot mint or revoke
 * keys), which is why managing them lives in the app and nowhere else.
 */

import type { components } from "@/lib/api-types";
import { apiFetch } from "./LibraryApi";

export type ApiKey = components["schemas"]["ApiKeyOut"];
export type ApiKeyCreate = components["schemas"]["ApiKeyCreate"];
export type ApiKeyCreated = components["schemas"]["ApiKeyCreated"];

export function listKeys(): Promise<ApiKey[]> {
  return apiFetch("/api/keys");
}

export function createKey(body: ApiKeyCreate): Promise<ApiKeyCreated> {
  return apiFetch("/api/keys", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function revokeKey(id: string): Promise<ApiKey> {
  return apiFetch(`/api/keys/${encodeURIComponent(id)}`, { method: "DELETE" });
}
