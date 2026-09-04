import { withSerwist } from "@serwist/turbopack";
import type { NextConfig } from "next";

// Local dev only: Vercel's service rewrite (vercel.json) sends /api/* to
// FastAPI in deployments; `next dev` has no such router, so forward to a
// local uvicorn.
//
// This MUST be a `fallback` rewrite, not the default (`afterFiles`) phase.
// afterFiles runs after static files but BEFORE dynamic routes, so a plain
// `/api/:path*` rewrite beats the Neon Auth catch-all at
// app/api/auth/[...path] — every `GET /api/auth/token` went to FastAPI, which
// 404s, so the client never got a JWT and every authenticated call failed.
// `fallback` runs last, after dynamic routes: /api/auth/* reaches its handler
// and everything with no Next route falls through to the backend.
const devApi = process.env.DEV_API_URL ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  agentRules: false,
  async rewrites() {
    const none = { beforeFiles: [], afterFiles: [], fallback: [] };
    if (process.env.NODE_ENV !== "development") return none;
    return {
      ...none,
      fallback: [{ source: "/api/:path*", destination: `${devApi}/api/:path*` }],
    };
  },
};

// Adds esbuild to serverExternalPackages so the /serwist route handler can
// bundle app/sw.ts at build time; the SW itself is configured in that route.
export default withSerwist(nextConfig);
