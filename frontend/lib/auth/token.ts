"use client";

import { authClient } from "@/lib/auth/client";

/**
 * The one JWT cache for the whole client (§2: the browser holds the Neon Auth
 * session cookie and sends a 15-min EdDSA JWT that FastAPI verifies offline
 * against the JWKS endpoint).
 *
 * Four identical copies of this lived in lib/api.ts, DiaryApi.tsx,
 * LibraryApi.tsx and PhotoApi.tsx, each with its own empty cache — and each
 * of those files' own comments asked for the collapse. The cost was real, not
 * cosmetic: Today's four mount reads span two of the copies, so a cold open
 * fetched the token twice, in series ahead of the data, before anything could
 * render.
 *
 * `authClient.token()` stays the SDK's, not ours. It is not a plain GET of a
 * token endpoint — it is `getSession()` with the JWT arriving in the
 * `set-auth-jwt` response header, behind the SDK's own sign-out invalidation
 * and cross-tab BroadcastChannel. Reimplementing that here would be writing an
 * auth path by hand, which is exactly what this app has no reason to do.
 * What IS ours is the 30-second freshness margin (never hand FastAPI a token
 * about to expire in flight) and the in-flight join below.
 *
 * Lifetime: module scope, so it dies with the document. Sign-out reloads the
 * page rather than routing (AccountCard.tsx), which is what makes that safe —
 * no token can outlive the account it belongs to.
 */

let cached: { token: string; exp: number } | null = null;
let inFlight: Promise<string | null> | null = null;

function jwtExp(token: string): number {
  try {
    const payload = JSON.parse(atob(token.split(".")[1] ?? ""));
    return typeof payload.exp === "number" ? payload.exp * 1000 : 0;
  } catch {
    return 0;
  }
}

/**
 * The current JWT, or null when there is no session.
 *
 * `force` is the 401 retry: it always starts a fresh call, because the token
 * an in-flight one would hand back may be the very one the server just
 * rejected. Ordinary callers join whatever call is already running — that is
 * what keeps a cold mount's concurrent reads to one round trip instead of one
 * each.
 */
export async function getToken(force: boolean): Promise<string | null> {
  if (!force && cached && cached.exp - Date.now() > 30_000) return cached.token;
  if (!force && inFlight) return inFlight;

  const run: Promise<string | null> = authClient
    .token()
    .then((res) => {
      const token = res.data?.token ?? null;
      // Never write an older token over a newer one. Now that every seam
      // shares this cache, a slow ordinary fetch can land *after* the forced
      // refresh that overtook it — putting back the token the server just
      // rejected, and re-arming that 401 for everything behind it. (No token
      // at all still clears: that means the session is gone.)
      const exp = token ? jwtExp(token) : 0;
      if (!token || !cached || exp >= cached.exp) {
        cached = token ? { token, exp } : null;
      }
      return token;
    })
    .finally(() => {
      if (inFlight === run) inFlight = null;
    });

  inFlight = run;
  return run;
}
