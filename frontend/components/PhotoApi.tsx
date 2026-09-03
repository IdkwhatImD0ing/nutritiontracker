"use client";

/**
 * M4 photo-lane seams (DESIGN.md §3.4b, §12 M4) — routes/photo.py's two
 * routes plus the §3.4b flag trio on the generated StagedItem. `multipart`
 * mirrors lib/api.ts's unexported copy (same Bearer scheme), like the other
 * *Api.tsx seams.
 *
 *   POST /api/logs/photo               multipart {image, id, captured_at, meal_hint?} → LogCreateResponse
 *   POST /api/resolve/confirm-estimate {log_id, item_index} → StagedItem
 */

import {
  ApiError,
  type LogCreateResponse,
  type Meal,
  type StagedItem,
} from "@/lib/api";
import { authClient } from "@/lib/auth/client";
import { apiFetch } from "./LibraryApi";

// ---------------------------------------------------------------------------
// the §3.4b flag: estimated rows render flagged and commit only once confirmed
// ---------------------------------------------------------------------------

/** models.py StagedItem.estimated, as generated. */
export type Estimated = StagedItem["estimated"];

/** models.py StagedItem (M4): `estimated` says what vision guessed —
 *  "portion" on a database-resolved food whose amount came from the photo,
 *  "unresolved" on a photo item with no database match: it carries NO
 *  numbers, cannot be confirmed or committed, and routes the user to Search.
 *  `estimate_note` is user-facing (what vision saw / why it is a guess);
 *  `estimate_confirmed` is set only by confirmEstimate(). */
export type EstimateFields = Partial<
  Pick<StagedItem, "estimated" | "estimate_note" | "estimate_confirmed">
>;

export type EstimateState = {
  estimated: Estimated;
  note: string | null;
  confirmed: boolean;
};

/** Read the trio off any staged row defensively — a pre-M4 payload restored
 *  from localStorage (PendingItems) carries none of it. */
export function readEstimate(item: StagedItem): EstimateState {
  // Read as a plain string: a pre-M4 row may carry nothing at all.
  const raw: string | undefined = item.estimated;
  return {
    estimated: raw === "portion" || raw === "unresolved" ? raw : "none",
    note:
      typeof item.estimate_note === "string" && item.estimate_note.trim()
        ? item.estimate_note
        : null,
    confirmed: item.estimate_confirmed === true,
  };
}

/** Stamp estimate fields onto a local row without touching anything else —
 *  the confirm response must not clobber local portion/meal edits (C3:
 *  editing the portion does not confirm; confirming does not undo the edit). */
export function withEstimate(item: StagedItem, fields: EstimateFields): StagedItem {
  return { ...item, ...fields };
}

/** C4: the commit transaction refuses with 409 "confirm the estimated rows
 *  first" while any estimated row is unconfirmed — belt and braces over the
 *  disabled Commit button. ApiError keeps the first 200 bytes of the body. */
export function isEstimateRefusal(e: unknown): boolean {
  return e instanceof ApiError && e.status === 409 && /estimat/i.test(e.message);
}

// ---------------------------------------------------------------------------
// fetch — Bearer <JWT> + one retry on 401, minus the JSON content-type
// (mirrors lib/api.ts multipart(): the browser sets the multipart boundary)
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

// ---------------------------------------------------------------------------
// routes
// ---------------------------------------------------------------------------

/** C2: POST /api/logs/photo — the (already client-downscaled) JPEG under
 *  `image` plus the log fields. `id` is the client uuid (§5 idempotent
 *  upsert: a retry of the same shot reuses it). `tz` rides along like the
 *  text and voice lanes send it; the route ignores it if it takes none. */
export async function createLogPhoto(args: {
  id: string;
  file: Blob;
  capturedAt: string;
  mealHint?: Meal;
}): Promise<LogCreateResponse> {
  const form = new FormData();
  form.set("id", args.id);
  form.set("captured_at", args.capturedAt);
  form.set("tz", Intl.DateTimeFormat().resolvedOptions().timeZone);
  if (args.mealHint) form.set("meal_hint", args.mealHint);
  form.set(
    "image",
    new File([args.file], "plate.jpg", { type: args.file.type || "image/jpeg" }),
  );
  const res = await multipart("/api/logs/photo", form);
  if (!res.ok) {
    throw new ApiError(res.status, await res.text().catch(() => ""));
  }
  return (await res.json()) as LogCreateResponse;
}

/** C3: POST /api/resolve/confirm-estimate — the explicit confirm on an
 *  estimated row; returns the row with estimate_confirmed=true. */
export function confirmEstimate(
  logId: string,
  itemIndex: number,
): Promise<StagedItem> {
  return apiFetch("/api/resolve/confirm-estimate", {
    method: "POST",
    body: JSON.stringify({ log_id: logId, item_index: itemIndex }),
  });
}
