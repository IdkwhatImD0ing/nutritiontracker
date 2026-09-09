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
  quotaRefusal,
  type LogCreateResponse,
  type Meal,
  type StagedItem,
} from "@/lib/api";
import { getToken } from "@/lib/auth/token";
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

/** What went wrong sending a plate, in the user's words. The upload's own
 *  refusals (415/413/422) are plain HTTP; a vision failure arrives as the
 *  stream's `error` event, which streamAgent re-throws with its status, so
 *  both land here. The demo quota's sentence is the server's own, verbatim
 *  (§13) — never paraphrased. Lives here, not in the capture sheet, because
 *  the photo lane's failures now surface in the chat (§6.4). */
export function photoErrorMessage(e: unknown): string {
  const quota = quotaRefusal(e);
  if (quota) return quota.detail;
  if (e instanceof ApiError) {
    if (e.status === 413) return "That photo is too large to send.";
    if (e.status === 400 || e.status === 415 || e.status === 422) {
      return "That image wasn't accepted — try a JPEG or PNG.";
    }
    if (e.status === 503) {
      return "Photo recognition is unavailable right now — retry, or type it.";
    }
  }
  return "Couldn't send that photo — check your connection and retry.";
}

// ---------------------------------------------------------------------------
// fetch — Bearer <JWT> + one retry on 401, minus the JSON content-type
// (mirrors lib/api.ts multipart(): the browser sets the multipart boundary)
// ---------------------------------------------------------------------------

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
 *  text and voice lanes send it; the route ignores it if it takes none.
 *
 *  The non-streaming entry, and the app no longer uses it: since 2026-09-08
 *  the + sheet's photo lane goes through the chat (lib/api.streamAgent with
 *  a shot), which runs the identical server-side pipeline and can then be
 *  talked to. Kept because the route is still there and still tested. */
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
