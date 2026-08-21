"use client";

import { createAuthClient } from "@neondatabase/auth/next";

/**
 * Browser-side auth client. `authClient.token()` returns the short-lived JWT
 * (15-min expiry) that lib/api.ts attaches as a Bearer header for FastAPI to
 * verify offline against the JWKS endpoint (§2).
 */
export const authClient = createAuthClient();
