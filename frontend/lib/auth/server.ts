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
 */
export function getAuth(): NeonAuth {
  if (!instance) {
    instance = createNeonAuth({
      baseUrl: process.env.NEON_AUTH_BASE_URL!,
      cookies: {
        secret: process.env.NEON_AUTH_COOKIE_SECRET!,
      },
    });
  }
  return instance;
}
