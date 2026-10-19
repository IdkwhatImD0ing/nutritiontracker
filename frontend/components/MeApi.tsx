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

import { useEffect, useSyncExternalStore } from "react";
import type { components } from "@/lib/api-types";
import { apiFetch } from "./LibraryApi";

export type DemoQuota = components["schemas"]["DemoQuota"];
export type Me = components["schemas"]["MeResponse"];

/* The newest /api/me this document has read, shared by every useMe(). Module
   state lasts as long as the document; signing out reloads it (AccountCard),
   which drops this with everything else. */
let pending: Promise<Me> | null = null;
let latest: Me | null = null;
const listeners = new Set<() => void>();

/** A fresh read, every call — the quota moves with each log, so Today asks
 *  again after one. Each read also becomes the shared answer: a useMe()
 *  mounted later reuses it instead of asking again. */
export function getMe(): Promise<Me> {
  const p = apiFetch<Me>("/api/me");
  pending = p;
  p.then(
    (me) => {
      if (pending !== p) return; // a newer read is on its way
      latest = me;
      listeners.forEach((l) => l());
    },
    () => {
      if (pending === p) pending = null; // the next useMe() asks again
    },
  );
  return p;
}

/** The newest read so far, or null: the first paint of a component that
 *  then reads fresh with getMe() for the quota (Today, AccountCard), so a
 *  return to it doesn't flash "…" or drop the demo banner. Null on a
 *  hard load, server and client alike, so it is safe as a useState
 *  initialiser. */
export function peekMe(): Me | null {
  return latest;
}

function subscribe(onChange: () => void) {
  listeners.add(onChange);
  return () => listeners.delete(onChange);
}

/** Who is signed in, for the facts that hold for the whole visit (is_owner,
 *  email). However many components ask, useMe() sends at most one GET
 *  /api/me per page load, and none when a getMe() already has. null until
 *  the first read lands; a failed read is retried by the next mount. A
 *  remount after that renders with it at once, no blank frame. For the demo
 *  quota, which a log spends, call getMe() where it changes. */
export function useMe(): Me | null {
  useEffect(() => {
    if (!pending) getMe().catch(() => {});
  }, []);
  return useSyncExternalStore(subscribe, () => latest, () => null);
}
