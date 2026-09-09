import { createNeonAuth } from "@neondatabase/auth/next/server";

type NeonAuth = ReturnType<typeof createNeonAuth>;

let instance: NeonAuth | null = null;

/**
 * Server-side Neon Auth singleton (§2): sessions live in the neon_auth schema
 * of our own database; this instance backs the API handler, the proxy
 * middleware, and the sign-in/sign-up server actions.
 *
 * Constructed lazily so `next build` (page-data collection evaluates route
 * modules) succeeds without NEON_AUTH_* secrets in the build environment.
 *
 * sessionDataTtl is the lifetime of the signed cookie that caches session data
 * so a request need not re-validate against the upstream Neon Auth server. The
 * SDK's default is 300s and the clock restarts on each miss, so the proxy —
 * which runs ahead of every protected navigation (proxy.ts) — pays that round
 * trip every five minutes, before any HTML can be produced.
 *
 * What 900s costs, stated plainly: a session revoked *upstream* (signed out on
 * another device, revoked by an admin) stays trusted by the web surface for up
 * to 15 minutes instead of 5. Three things bound that. The SDK clamps the
 * cookie's expiry to `Math.min(session.expiresAt, now + ttl)`, so the cache can
 * never outlive the session itself. Signing out *here* is not affected at all:
 * the sign-out response deletes the session token and the SDK emits a
 * `maxAge: 0` clear for this cookie in the same response, so it is invalidated
 * by deletion, not by expiry. And 15 minutes is already the window FastAPI
 * grants a JWT that has been handed out (§2), so the API side does not get
 * shorter by keeping this at 5. It does not touch the session *token* cookie's
 * TTL, only how long a cached copy of the session data is trusted.
 */
export function getAuth(): NeonAuth {
  if (!instance) {
    instance = createNeonAuth({
      baseUrl: process.env.NEON_AUTH_BASE_URL!,
      cookies: {
        secret: process.env.NEON_AUTH_COOKIE_SECRET!,
        sessionDataTtl: 900,
      },
    });
  }
  return instance;
}
