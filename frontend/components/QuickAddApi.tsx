"use client";

/**
 * Quick-add seams — the + sheet's Scan barcode and Search lanes (owner
 * ruling, 2026-09-02). Both routes live in backend/routes/quick.py; the
 * shapes are the generated ones (lib/api-types.ts). `apiFetch` is
 * LibraryApi's Bearer + single-401-retry helper (same scheme as lib/api.ts's
 * request()).
 *
 *   GET  /api/foods/barcode?code=   → BarcodeLookup
 *   POST /api/logs/from-food        → LogResponse (one resolved staged row)
 *
 * Neither lane learns anything: a row the user never spoke writes no alias
 * and no portion default (commit reads logs.input_mode).
 */

import type { LogResponse } from "@/lib/api";
import type { components } from "@/lib/api-types";
import { apiFetch } from "./LibraryApi";

export type BarcodeFood = components["schemas"]["BarcodeFood"];

/** BarcodeResponse, narrowed: `found` decides whether `food` is there. */
export type BarcodeLookup =
  | { found: true; food: BarcodeFood }
  | { found: false; food: null };

/** Digits only — the server strips non-digits too, and then runs the §3.4
 *  gtinUpc normalization ladder (as-scanned → zeros stripped → 13/14-padded). */
export function lookupBarcode(code: string): Promise<BarcodeLookup> {
  const qs = new URLSearchParams({ code: code.replace(/\D/g, "") });
  return apiFetch(`/api/foods/barcode?${qs.toString()}`);
}

/** `id` is the client uuid (§5 idempotent upsert, so a retry can't create
 *  two logs); serving_id null → the food's first serving; qty null → 1. */
export type LogFromFood = components["schemas"]["FromFoodRequest"];

export function createLogFromFood(body: LogFromFood): Promise<LogResponse> {
  return apiFetch("/api/logs/from-food", {
    method: "POST",
    body: JSON.stringify(body),
  });
}
